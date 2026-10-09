#!/usr/bin/env python3
"""批量运行 Judge challenge set，并按失败形态报告 claim 与排序敏感性。"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from trust.challenge_set import SCHEMA
from trust.evaluate import evaluate_trace
from trust.evidence_audit import audit_challenge_evidence
from trust.judge import CLAIM_NAMES, SYSTEM_PROMPT
from trust.providers import OpenAIProvider


def behavior_contract_gate(*, records: int, counterfactuals: int,
                           provider_errors: int, claim_exact_accuracy: float | None,
                           ordering_sensitivity: float | None, ordering_checked: int,
                           families: dict) -> dict:
    """合成 challenge 的可证明行为门禁；阈值不是部署准确率要求。"""
    failures = []
    if provider_errors:
        failures.append("provider-errors")
    if claim_exact_accuracy != 1.0:
        failures.append("claim-not-exact")
    if ordering_checked != counterfactuals:
        failures.append("not-all-counterfactuals-checked")
    if counterfactuals and ordering_sensitivity != 1.0:
        failures.append("counterfactual-ordering-not-monotonic")
    failed_families = sorted(
        family for family, values in families.items()
        if values.get("orderingChecked", 0) > 0
        and values.get("orderingSensitivity") != 1.0)
    if failed_families:
        failures.append("family-ordering-not-monotonic")
    return {"passed": not failures, "failures": failures,
            "failedFamilies": failed_families,
            "coverage": {"records": records, "counterfactuals": counterfactuals,
                         "orderingChecked": ordering_checked},
            "policy": "exact synthetic claims and strict counterfactual monotonicity",
            "interpretation": "deterministic challenge contract; not deployment accuracy"}


def read_challenges(path: str | Path) -> list[dict]:
    rows = []
    for line_no, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{path}:{line_no}: {error}") from error
        if row.get("schema") != SCHEMA:
            raise ValueError(f"{path}:{line_no}: 不支持的 challenge schema")
        rows.append(row)
    return rows


def _claim_comparison(challenge: dict, evaluation: dict) -> tuple[int, int, list[dict]]:
    expected = {row["nodeId"]: row for row in challenge["expected"]["stepClaims"]}
    actual = {row["nodeId"]: row.get("claims") or {} for row in evaluation["steps"]}
    correct, total, differences = 0, 0, []
    for node_id, expected_row in expected.items():
        for claim in CLAIM_NAMES:
            total += 1
            observed = (actual.get(node_id, {}).get(claim) or {}).get("verdict")
            wanted = expected_row[claim]
            correct += observed == wanted
            if observed != wanted:
                differences.append({"nodeId": node_id, "claim": claim,
                                    "expected": wanted, "observed": observed})
    return correct, total, differences


def _model_outputs(evaluation: dict) -> list[dict]:
    return [{"nodeId": step["nodeId"], "scores": step["scores"],
             "scoreEvidence": step["scoreEvidence"],
             "overall": step["overall"], "label": step["label"],
             "claims": step.get("claims", {}), "reason": step["reason"]}
            for step in evaluation["steps"]]


def run_challenges(challenges: list[dict], *, provider, goal: str,
                   system_prompt: str = SYSTEM_PROMPT, prompt_id: str = "canonical",
                   cache_dir: str | Path | None = None) -> dict:
    ids = [row["id"] for row in challenges]
    if len(ids) != len(set(ids)):
        raise ValueError("challenge id 不唯一")
    runs = []
    for challenge in challenges:
        value = challenge["input"]
        try:
            evaluated = evaluate_trace(
                value["trace"], goal=goal, provider=provider,
                reference=value.get("reference"), observations=value.get("observations"),
                oracle_spec=value.get("oracleSpec"), system_prompt=system_prompt,
                cache_dir=cache_dir)
            correct, total, differences = _claim_comparison(challenge, evaluated["judge"])
            runs.append({"id": challenge["id"], "baselineId": challenge["baselineId"],
                         "family": challenge["failureFamily"], "mutation": challenge["mutation"],
                         "status": "success", "claimCorrect": correct, "claimTotal": total,
                         "claimRecordExact": correct == total,
                         "modelOutputs": _model_outputs(evaluated["judge"]),
                         "trajectory": evaluated["judge"]["trajectory"],
                         "hybridTrajectory": evaluated["trajectory"],
                         "claimDifferences": differences,
                         "judgeOrderingScore": evaluated["aggregate"]["judgeOrderingScore"],
                         "hybridOrderingScore": evaluated["aggregate"]["hybridOrderingScore"],
                         "rubricVersion": evaluated["provenance"].get("rubricVersion"),
                         "promptHash": evaluated["provenance"].get("promptHash"),
                         "promptId": prompt_id,
                         "provider": evaluated["provenance"].get("provider")})
        except Exception as error:  # 单个错误必须留在报告里，不能让批处理吞掉其余样本。
            runs.append({"id": challenge["id"], "baselineId": challenge["baselineId"],
                         "family": challenge["failureFamily"], "mutation": challenge["mutation"],
                         "status": "provider-error", "errorType": type(error).__name__,
                         "error": str(error), "claimCorrect": 0,
                         "claimTotal": len(challenge["expected"]["stepClaims"]) * len(CLAIM_NAMES),
                         "claimRecordExact": False, "modelOutputs": [], "trajectory": None})

    by_id = {row["id"]: row for row in runs}
    for row in runs:
        if row["baselineId"] is None or row["status"] != "success":
            row["orderingDecreased"] = None
            continue
        baseline = by_id.get(row["baselineId"])
        row["orderingDecreased"] = bool(
            baseline and baseline["status"] == "success"
            and row["hybridOrderingScore"] < baseline["hybridOrderingScore"])

    groups = defaultdict(list)
    for row in runs:
        groups[row["family"]].append(row)
    families = {}
    for family, rows in sorted(groups.items()):
        claim_total = sum(row["claimTotal"] for row in rows)
        claim_correct = sum(row["claimCorrect"] for row in rows)
        counterfactuals = [row for row in rows if row["baselineId"] is not None]
        known_sensitivity = [row["orderingDecreased"] for row in counterfactuals
                             if row["orderingDecreased"] is not None]
        families[family] = {
            "runs": len(rows),
            "providerErrors": sum(row["status"] != "success" for row in rows),
            "claimExactAccuracy": round(claim_correct / claim_total, 4) if claim_total else None,
            "orderingSensitivity": (round(sum(known_sensitivity) / len(known_sensitivity), 4)
                                    if known_sensitivity else None),
            "orderingChecked": len(known_sensitivity),
        }
    claim_total = sum(row["claimTotal"] for row in runs)
    claim_correct = sum(row["claimCorrect"] for row in runs)
    sensitivities = [row["orderingDecreased"] for row in runs
                     if row["orderingDecreased"] is not None]
    prompt_hash = hashlib.sha256(system_prompt.encode()).hexdigest()
    evidence_audit = audit_challenge_evidence(challenges, runs)
    provider_errors = sum(row["status"] != "success" for row in runs)
    claim_exact_accuracy = round(claim_correct / claim_total, 4) if claim_total else None
    ordering_sensitivity = (round(sum(sensitivities) / len(sensitivities), 4)
                            if sensitivities else None)
    counterfactuals = sum(row["baselineId"] is not None for row in runs)
    behavior_gate = behavior_contract_gate(
        records=len(runs), counterfactuals=counterfactuals,
        provider_errors=provider_errors, claim_exact_accuracy=claim_exact_accuracy,
        ordering_sensitivity=ordering_sensitivity, ordering_checked=len(sensitivities),
        families=families)
    return {"provider": provider.identity, "promptId": prompt_id,
            "modelOutputSource": "validated-judge-before-hybrid",
            "promptHash": prompt_hash, "runs": runs, "byFailureFamily": families,
            "evidenceAudit": evidence_audit, "behaviorGate": behavior_gate,
            "aggregate": {
                "records": len(runs),
                "providerErrors": provider_errors,
                "claimExactAccuracy": claim_exact_accuracy,
                "orderingSensitivity": ordering_sensitivity,
                "orderingChecked": len(sensitivities),
                "confidence": "synthetic-counterfactual-benchmark",
                "interpretation": "Oracle-conditioned validated Judge claims and raw evidence audit; hybrid ordering sensitivity; not independent model accuracy or success probability",
            }}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("challenges")
    parser.add_argument("--goal", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning-effort", default="low")
    parser.add_argument("--cache-dir", default=".trust-judge-cache")
    parser.add_argument("--no-cache", action="store_true",
                        help="独立随机 trial 时禁用 Judge cache")
    parser.add_argument("--system-prompt-file")
    parser.add_argument("--prompt-id", default="canonical")
    args = parser.parse_args(argv)
    report = run_challenges(
        read_challenges(args.challenges), goal=args.goal,
        provider=OpenAIProvider(args.model, reasoning_effort=args.reasoning_effort),
        system_prompt=(Path(args.system_prompt_file).read_text(encoding="utf-8")
                       if args.system_prompt_file else SYSTEM_PROMPT),
        prompt_id=args.prompt_id,
        cache_dir=None if args.no_cache else args.cache_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if (not report["behaviorGate"]["passed"]
                 or not report["evidenceAudit"]["contractGate"]["passed"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
