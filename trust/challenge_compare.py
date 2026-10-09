#!/usr/bin/env python3
"""对两个 Judge challenge report 做配对回归与显著性审计。"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def _exact_sign_p(wins: int, losses: int) -> float | None:
    """H0: 配对不一致样本中胜负各 0.5；双侧 exact binomial。"""
    n = wins + losses
    if n == 0:
        return None
    tail = sum(math.comb(n, k) for k in range(0, min(wins, losses) + 1)) / (2 ** n)
    return round(min(1.0, 2 * tail), 6)


def _paired_metric(pairs: list[tuple[dict, dict]], key: str, *, minimum: int,
                   alpha: float) -> dict:
    usable = [(bool(left[key]), bool(right[key])) for left, right in pairs
              if left.get(key) is not None and right.get(key) is not None]
    wins = sum(not left and right for left, right in usable)
    losses = sum(left and not right for left, right in usable)
    ties = len(usable) - wins - losses
    baseline_rate = sum(left for left, _ in usable) / len(usable) if usable else None
    candidate_rate = sum(right for _, right in usable) / len(usable) if usable else None
    delta = candidate_rate - baseline_rate if usable else None
    p_value = _exact_sign_p(wins, losses)
    if len(usable) < minimum:
        status = "insufficient-data"
    elif losses > wins:
        status = "regression"
    elif wins > losses and p_value is not None and p_value <= alpha:
        status = "significant-improvement"
    else:
        status = "no-significant-change"
    return {"pairedSamples": len(usable), "minimumSamples": minimum,
            "baselineRate": round(baseline_rate, 4) if baseline_rate is not None else None,
            "candidateRate": round(candidate_rate, 4) if candidate_rate is not None else None,
            "delta": round(delta, 4) if delta is not None else None,
            "wins": wins, "losses": losses, "ties": ties,
            "exactSignPValue": p_value, "alpha": alpha, "status": status,
            "passed": status not in {"regression", "insufficient-data"}}


def compare_challenge_reports(baseline: dict, candidate: dict, *,
                              minimum_samples: int = 20,
                              minimum_family_samples: int = 5,
                              alpha: float = 0.05) -> dict:
    if not 0 < alpha < 1:
        raise ValueError("alpha 必须在 (0, 1)")
    if minimum_samples < 1 or minimum_family_samples < 1:
        raise ValueError("minimum samples 必须为正整数")
    left, right = ({row["id"]: row for row in report.get("runs", [])}
                   for report in (baseline, candidate))
    missing = {"missingInCandidate": sorted(set(left) - set(right)),
               "newInCandidate": sorted(set(right) - set(left))}
    common = sorted(set(left) & set(right))
    pairs = [(left[key], right[key]) for key in common]
    by_family = {}
    for family in sorted({row["family"] for pair in pairs for row in pair}):
        family_pairs = [pair for pair in pairs if pair[0]["family"] == pair[1]["family"] == family]
        by_family[family] = {
            "claimRecordExact": _paired_metric(family_pairs, "claimRecordExact",
                                                minimum=minimum_family_samples, alpha=alpha),
            "orderingDetection": _paired_metric(
                [pair for pair in family_pairs if pair[0].get("baselineId") is not None],
                "orderingDecreased", minimum=minimum_family_samples, alpha=alpha),
        }
    metrics = {
        "claimRecordExact": _paired_metric(pairs, "claimRecordExact",
                                            minimum=minimum_samples, alpha=alpha),
        "orderingDetection": _paired_metric(
            [pair for pair in pairs if pair[0].get("baselineId") is not None],
            "orderingDecreased", minimum=minimum_samples, alpha=alpha),
    }
    baseline_errors = sum(row.get("status") != "success" for row in left.values())
    candidate_errors = sum(row.get("status") != "success" for row in right.values())
    gates = {
        "sameChallengeIds": not missing["missingInCandidate"] and not missing["newInCandidate"],
        "samePromptHash": (baseline.get("promptHash") is not None
                           and baseline.get("promptHash") == candidate.get("promptHash")),
        "providerErrorsNotIncreased": candidate_errors <= baseline_errors,
        "aggregateMetricsPassed": all(row["passed"] for row in metrics.values()),
        "noFamilyRegression": all(
            metric["status"] != "regression"
            for family in by_family.values() for metric in family.values()),
    }
    return {"passed": all(gates.values()), "gates": gates, "challengeSetDiff": missing,
            "providerErrors": {"baseline": baseline_errors, "candidate": candidate_errors},
            "aggregate": metrics, "byFailureFamily": by_family,
            "interpretation": ("paired exact comparison; improvement requires significance, "
                               "regression fails conservatively; not a success probability")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline")
    parser.add_argument("candidate")
    parser.add_argument("--minimum-samples", type=int, default=20)
    parser.add_argument("--minimum-family-samples", type=int, default=5)
    parser.add_argument("--alpha", type=float, default=0.05)
    args = parser.parse_args(argv)
    load = lambda path: json.loads(Path(path).read_text(encoding="utf-8"))
    report = compare_challenge_reports(
        load(args.baseline), load(args.candidate), minimum_samples=args.minimum_samples,
        minimum_family_samples=args.minimum_family_samples, alpha=args.alpha)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
