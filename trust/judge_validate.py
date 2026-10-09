#!/usr/bin/env python3
"""Judge 的 meta-eval：结构、变异敏感性、一致性与硬规则冲突。"""

from __future__ import annotations

import argparse
import statistics
import json
from pathlib import Path
from typing import Any

from trust.hybrid import hybrid_evaluation
from trust.judge import judge_trace
from trust.mutate import MUTATIONS, mutate
from trust.step_score import score_steps
from trust.providers import OpenAIProvider
from trust.observation_mutate import (OBSERVATION_MUTATIONS, complete_observations,
                                      mutate_observation)
from trust.oracle import evaluate_test_case


def failure_node_hits(cases: dict[str, tuple[dict, dict]], labels: dict[str, dict]) -> dict:
    """实测失败节点是否被规则 finding 或 hybrid 低分明确点出。"""
    rows = []
    for case, record in labels.items():
        if record.get("label") not in ("stable-red", "flip") or case not in cases:
            continue
        failed = next((run.get("failedNode") for run in record.get("runs", [])
                       if run.get("failedNode")), None)
        if not failed:
            continue
        trace, judged = cases[case]
        hybrid = hybrid_evaluation(trace, judged)
        step = next((item for item in hybrid["steps"] if item["nodeId"] == failed), None)
        hit = bool(step and (step["findings"] or step["label"] in ("risky", "fail")))
        rows.append({"case": case, "failedNode": failed, "hit": hit,
                     "label": step["label"] if step else None})
    return {"hits": sum(row["hit"] for row in rows), "checked": len(rows), "rows": rows}


def calibration_rows(cases: dict[str, tuple[dict, dict]], labels: dict[str, dict]) -> list[dict]:
    """导出后续人工标注/校准所需原始量；不在这里伪造校准概率。"""
    rows = []
    for case, (trace, judged) in cases.items():
        hybrid = hybrid_evaluation(trace, judged)
        label = labels.get(case) or {}
        latest_observations = next((run.get("observations") for run in reversed(label.get("runs", []))
                                    if run.get("observations")), None)
        oracle = evaluate_test_case(trace, trace, observations=latest_observations)
        rows.append({
            "case": case, "targetLabel": label.get("label"),
            "failedNode": next((run.get("failedNode") for run in label.get("runs", [])
                                if run.get("failedNode")), None),
            "deterministicOrderingScore": hybrid["aggregate"]["deterministicOrderingScore"],
            "judgeOrderingScore": hybrid["aggregate"]["judgeOrderingScore"],
            "hybridOrderingScore": hybrid["aggregate"]["hybridOrderingScore"],
            "stepScores": [{"nodeId": step["nodeId"], "scores": step["scores"],
                            "scoreEvidence": step["scoreEvidence"],
                            "label": step["label"], "claims": step.get("claims", {})}
                           for step in hybrid["steps"]],
            "oracleSummary": oracle["summary"],
            "rubricVersion": judged.get("provenance", {}).get("rubricVersion"),
            "provider": judged.get("provenance", {}).get("provider"),
            "confidence": "uncalibrated-judge-score",
        })
    return rows


def write_calibration_jsonl(path: str | Path, rows: list[dict]) -> None:
    Path(path).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                          encoding="utf-8")


def validate_judge(base_trace: dict, *, goal: str, provider, repetitions: int = 2) -> dict[str, Any]:
    if repetitions < 1:
        raise ValueError("repetitions 必须至少为 1")
    runs = [judge_trace(base_trace, goal=goal, provider=provider) for _ in range(repetitions)]
    hybrids = [hybrid_evaluation(base_trace, run) for run in runs]
    labels = [[step["label"] for step in run["steps"]] for run in runs]
    label_consistency = (sum(row == labels[0] for row in labels) / len(labels)) if labels else 1.0
    score_columns = zip(*[[step["overall"] for step in run["steps"]] for run in runs])
    variances = [statistics.pvariance(column) for column in score_columns]

    rule_steps = {step["nodeId"]: step for step in score_steps(base_trace)["steps"]}
    raw_conflicts = 0
    opportunities = 0
    for model_step in runs[0]["steps"]:
        rule = rule_steps[model_step["nodeId"]]
        for key, value in rule["scores"].items():
            if value < 1:
                opportunities += 1
                raw_conflicts += model_step["scores"][key] > value
    enforced_conflicts = 0
    for step in hybrids[0]["steps"]:
        rule = rule_steps[step["nodeId"]]
        enforced_conflicts += any(step["scores"][key] > rule["scores"][key]
                                  for key in rule["scores"])

    base_score = hybrids[0]["aggregate"]["hybridOrderingScore"]
    mutation_rows = []
    for name in MUTATIONS:
        changed = mutate(base_trace, name)
        if changed is None:
            continue
        mutated, description = changed
        judged = judge_trace(mutated, goal=goal, provider=provider)
        score = hybrid_evaluation(mutated, judged)["aggregate"]["hybridOrderingScore"]
        mutation_rows.append({"mutation": name, "description": description,
                              "before": base_score, "after": score,
                              "decreased": score < base_score})

    complete = complete_observations(base_trace)
    oracle_base = evaluate_test_case(base_trace, base_trace, observations=complete)["summary"]
    observation_rows = []
    for name in OBSERVATION_MUTATIONS:
        changed, description = mutate_observation(base_trace, complete, name)
        summary = evaluate_test_case(base_trace, base_trace, observations=changed)["summary"]
        before = oracle_base["evidenceAdjustedOrderingScore"]
        after = summary["evidenceAdjustedOrderingScore"]
        observation_rows.append({"mutation": name, "description": description,
                                 "before": before, "after": after,
                                 "decreased": after < before})

    return {
        "structure": {"valid": True, "runs": len(runs)},
        "mutationSensitivity": {
            "passed": sum(row["decreased"] for row in mutation_rows),
            "checked": len(mutation_rows), "rows": mutation_rows,
        },
        "oracleCounterfactualSensitivity": {
            "passed": sum(row["decreased"] for row in observation_rows),
            "checked": len(observation_rows), "rows": observation_rows,
        },
        "repeatConsistency": {"labelAgreement": round(label_consistency, 4),
                              "maxScoreVariance": max(variances, default=0)},
        "ruleConflicts": {"raw": raw_conflicts, "opportunities": opportunities,
                          "afterConservativeMerge": enforced_conflicts},
        "interpretation": "meta-eval diagnostics; not model accuracy or calibration",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace")
    parser.add_argument("--goal", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--repetitions", type=int, default=2)
    args = parser.parse_args(argv)
    path = Path(args.trace)
    path = path / "trace.json" if path.is_dir() else path
    trace = json.loads(path.read_text(encoding="utf-8"))
    report = validate_judge(trace, goal=args.goal, provider=OpenAIProvider(args.model),
                            repetitions=args.repetitions)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
