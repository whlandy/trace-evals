#!/usr/bin/env python3
"""对语义等价 Judge rubric prompt 变体运行并分析鲁棒性。"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from trust.challenge_run import read_challenges, run_challenges
from trust.judge import RUBRIC_VERSION
from trust.providers import OpenAIProvider

SCHEMA = "trace-eval.prompt-variants/v1"
REQUIRED_INVARIANTS = {
    "evidence-only", "separate-claims", "oracle-non-override", "unknown-abstention",
    "evidence-backlink", "non-probability", "strict-json",
}


def validate_prompt_bundle(bundle: dict) -> list[dict]:
    if not isinstance(bundle, dict) or set(bundle) != {"schema", "rubricVersion", "variants"}:
        raise ValueError("prompt bundle 顶层字段必须严格为 schema/rubricVersion/variants")
    if bundle["schema"] != SCHEMA or bundle["rubricVersion"] != RUBRIC_VERSION:
        raise ValueError("prompt bundle schema 或 rubricVersion 不匹配")
    variants = bundle["variants"]
    if not isinstance(variants, list) or len(variants) < 2:
        raise ValueError("prompt robustness 至少需要两个 prompt 变体")
    ids, hashes = [], []
    for variant in variants:
        if not isinstance(variant, dict) or set(variant) != {
                "id", "prompt", "certifiedEquivalent", "invariants"}:
            raise ValueError("prompt variant 字段不合法")
        if not isinstance(variant["id"], str) or not variant["id"].strip():
            raise ValueError("prompt variant id 必须非空")
        if not isinstance(variant["prompt"], str) or not variant["prompt"].strip():
            raise ValueError("prompt variant prompt 必须非空")
        if variant["certifiedEquivalent"] is not True:
            raise ValueError(f"prompt variant {variant['id']} 未经等价性审核")
        if set(variant["invariants"]) != REQUIRED_INVARIANTS:
            raise ValueError(f"prompt variant {variant['id']} invariants 不完整")
        ids.append(variant["id"])
        hashes.append(hashlib.sha256(variant["prompt"].encode()).hexdigest())
    if len(ids) != len(set(ids)) or len(hashes) != len(set(hashes)):
        raise ValueError("prompt variant id 和文本必须唯一")
    return variants


def run_prompt_variants(challenges: list[dict], bundle: dict, *, provider, goal: str,
                        cache_dir: str | Path | None = None) -> list[dict]:
    variants = validate_prompt_bundle(bundle)
    return [run_challenges(challenges, provider=provider, goal=goal,
                           system_prompt=variant["prompt"], prompt_id=variant["id"],
                           cache_dir=cache_dir)
            for variant in variants]


def _summarize(rows: list[dict], *, score_range_threshold: float) -> dict:
    counterfactuals = [row for row in rows if row["baselineId"] is not None]
    return {
        "records": len(rows),
        "worstCaseClaimExactRate": (round(sum(all(row["claimOutcomes"]) for row in rows)
                                           / len(rows), 4) if rows else None),
        "claimFlipRate": (round(sum(len(set(row["claimOutcomes"])) > 1 for row in rows)
                               / len(rows), 4) if rows else None),
        "worstCaseOrderingSensitivity": (
            round(sum(all(row["orderingOutcomes"]) for row in counterfactuals)
                  / len(counterfactuals), 4) if counterfactuals else None),
        "orderingFlipRate": (
            round(sum(len(set(row["orderingOutcomes"])) > 1 for row in counterfactuals)
                  / len(counterfactuals), 4) if counterfactuals else None),
        "meanJudgeScoreRange": (round(sum(row["judgeScoreRange"] for row in rows)
                                           / len(rows), 4) if rows else None),
        "scoreRangeExceedances": sum(row["judgeScoreRange"] > score_range_threshold
                                     for row in rows),
        "providerErrors": sum(row["providerErrors"] for row in rows),
    }


def analyze_prompt_robustness(reports: list[dict], *,
                              score_range_threshold: float = 0.1) -> dict:
    if len(reports) < 2:
        raise ValueError("至少需要两个 prompt variant report")
    if score_range_threshold < 0:
        raise ValueError("score_range_threshold 不能为负")
    providers = {report.get("provider") for report in reports}
    prompt_ids = [report.get("promptId") for report in reports]
    prompt_hashes = [report.get("promptHash") for report in reports]
    if len(providers) != 1:
        raise ValueError("prompt variants 必须使用同一 provider")
    if (None in prompt_ids or len(prompt_ids) != len(set(prompt_ids))
            or None in prompt_hashes or len(prompt_hashes) != len(set(prompt_hashes))):
        raise ValueError("promptId/promptHash 必须存在且唯一")
    indexes = [{row["id"]: row for row in report["runs"]} for report in reports]
    if any(set(index) != set(indexes[0]) for index in indexes[1:]):
        raise ValueError("prompt variant challenge IDs 不一致")
    rows = []
    for challenge_id in sorted(indexes[0]):
        variants = [index[challenge_id] for index in indexes]
        families = {row["family"] for row in variants}
        if len(families) != 1:
            raise ValueError(f"failure family 不一致：{challenge_id}")
        scores = [row.get("judgeOrderingScore") for row in variants
                  if row.get("judgeOrderingScore") is not None]
        rows.append({"id": challenge_id, "family": next(iter(families)),
                     "baselineId": variants[0].get("baselineId"),
                     "claimOutcomes": [row.get("status") == "success"
                                       and row.get("claimRecordExact") is True for row in variants],
                     "orderingOutcomes": [row.get("orderingDecreased") is True
                                          for row in variants]
                     if variants[0].get("baselineId") is not None else [],
                     "judgeScores": scores,
                     "judgeScoreRange": round(max(scores) - min(scores), 4) if scores else 0.0,
                     "providerErrors": sum(row.get("status") != "success" for row in variants)})
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["family"]].append(row)
    aggregate = _summarize(rows, score_range_threshold=score_range_threshold)
    aggregate["robust"] = (aggregate["providerErrors"] == 0
                           and aggregate["claimFlipRate"] == 0
                           and aggregate["orderingFlipRate"] == 0
                           and aggregate["scoreRangeExceedances"] == 0)
    return {"provider": next(iter(providers)), "variants": [
                {"id": prompt_id, "hash": prompt_hash}
                for prompt_id, prompt_hash in zip(prompt_ids, prompt_hashes)],
            "records": rows, "aggregate": aggregate,
            "byFailureFamily": {family: _summarize(values,
                                                    score_range_threshold=score_range_threshold)
                                for family, values in sorted(grouped.items())},
            "scoreRangeThreshold": score_range_threshold,
            "interpretation": ("worst-case and invariance across human-certified equivalent prompts; "
                               "not calibrated deployment probability")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("challenges")
    parser.add_argument("prompt_bundle")
    parser.add_argument("--goal", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning-effort", default="low")
    parser.add_argument("--cache-dir", default=".trust-judge-cache")
    parser.add_argument("--score-range-threshold", type=float, default=0.1)
    args = parser.parse_args(argv)
    bundle = json.loads(Path(args.prompt_bundle).read_text(encoding="utf-8"))
    reports = run_prompt_variants(
        read_challenges(args.challenges), bundle, goal=args.goal,
        provider=OpenAIProvider(args.model, reasoning_effort=args.reasoning_effort),
        cache_dir=args.cache_dir)
    result = analyze_prompt_robustness(reports,
                                       score_range_threshold=args.score_range_threshold)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["aggregate"]["robust"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
