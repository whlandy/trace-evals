#!/usr/bin/env python3
"""把现有确定性 findings 映射成逐步骤、多维、仍可追溯的排序分数。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trust.eval_types import SCORE_DIMENSIONS, label_for, validate_step_evaluation  # noqa: E402
from trust.features import trace_features                                      # noqa: E402
from trust.rules import trace_findings                                      # noqa: E402

# 只表达失败形态的相对影响，不是概率。后续只能用新标签校准，不能手调成“准确率”。
DEDUCTION = {"silent-pass": 1.0, "loud-later": 0.6, "flaky": 0.4, "weak": 0.2}
AXIS_DIMENSIONS = {
    "replay": ("replay_safety", "context_fit"),
    "evidence": ("evidence_quality",),
    "observe": ("argument_quality",),
}


def _step(node_id: str, action: str | None, findings: list[dict]) -> dict:
    scores = {key: 1.0 for key in SCORE_DIMENSIONS}
    # 缺少动作本身不是现有规则的 finding，但对一个普通步骤是可确定的结构缺陷。
    if not action:
        scores["action_correctness"] = 0.0

    for finding in findings:
        deduction = DEDUCTION[finding["failure"]]
        for dimension in AXIS_DIMENSIONS[finding["axis"]]:
            scores[dimension] = min(scores[dimension], 1.0 - deduction)

    overall = round(sum(scores.values()) / len(scores), 4)
    score_evidence = {}
    for dimension in SCORE_DIMENSIONS:
        relevant = [finding for finding in findings
                    if dimension in AXIS_DIMENSIONS[finding["axis"]]]
        score_evidence[dimension] = {
            "evidence": ([finding["evidence"] for finding in relevant]
                         or [f"nodeId={node_id}", "deterministic:no-known-defect"]),
            "reason": ("；".join(finding["consequence"] for finding in relevant)
                       or "未命中该维度的已知确定性缺陷"),
        }
    result = {
        "nodeId": node_id, "scores": scores, "scoreEvidence": score_evidence,
        "overall": overall,
        "label": label_for(overall),
        "confidence": "deterministic",
        "findings": findings,
        "reason": "; ".join(f["evidence"] for f in findings) or "未命中已知确定性缺陷",
        "source": "rules",
        "grader": {"name": "trust.step_score", "version": "1"},
    }
    validate_step_evaluation(result)
    return result


def score_steps(trace: dict) -> dict:
    extracted = trace_findings(trace)
    node_facts = trace_features(trace)["nodes"]
    by_node: dict[str, list[dict]] = {node["nodeId"]: [] for node in node_facts}
    trace_level = []
    for finding in extracted["findings"]:
        if finding["node"] in by_node:
            by_node[finding["node"]].append(finding)
        else:
            trace_level.append(finding)

    steps = []
    for node in node_facts:
        node_id = node["nodeId"]
        steps.append(_step(node_id, node.get("action"), by_node[node_id]))

    values = [step["overall"] for step in steps]
    bottom = sorted(values)[:min(3, len(values))]
    aggregate = {
        "mean": round(sum(values) / len(values), 4) if values else None,
        "minimum": min(values) if values else None,
        "bottomKMean": round(sum(bottom) / len(bottom), 4) if bottom else None,
        "confidence": "deterministic",
        "interpretation": "uncalibrated-ordering-only",
    }
    return {
        "name": extracted["name"],
        "steps": steps,
        "traceFindings": trace_level,
        "aggregate": aggregate,
    }


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for arg in argv:
        path = Path(arg)
        path = path / "trace.json" if path.is_dir() else path
        print(json.dumps(score_steps(json.loads(path.read_text(encoding="utf-8"))),
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
