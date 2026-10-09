"""确定性规则与模型 judge 的保守组合；模型永远不能抬高硬规则维度。"""

from __future__ import annotations

import math

from trust.eval_types import SCORE_DIMENSIONS, label_for, validate_step_evaluation
from trust.score import score_trace
from trust.step_score import score_steps
from trust.oracle_caps import oracle_score_caps


def _cap(findings: list[dict]) -> float:
    failures = {finding["failure"] for finding in findings}
    if "silent-pass" in failures:
        return 0.49
    if "loud-later" in failures:
        return 0.74
    return 1.0


def _cap_evidence(checks: list[dict]) -> list[str]:
    return list(dict.fromkeys(
        token for check in checks
        for token in [check.get("criterion"), *check.get("evidence", [])]
        if isinstance(token, str) and token.strip()))


def hybrid_evaluation(trace: dict, judge: dict, *, oracle: dict | None = None) -> dict:
    deterministic = score_steps(trace)
    oracle_caps = oracle_score_caps(oracle)
    judge_by_id = {step["nodeId"]: step for step in judge["steps"]}
    steps = []
    for rule_step in deterministic["steps"]:
        model_step = judge_by_id.get(rule_step["nodeId"])
        if model_step is None:
            raise ValueError(f"judge 缺少节点 {rule_step['nodeId']}")
        scores = {key: min(rule_step["scores"][key], model_step["scores"][key])
                  for key in SCORE_DIMENSIONS}
        node_caps = oracle_caps["steps"].get(rule_step["nodeId"], {})
        for key in node_caps:
            scores[key] = 0.0
        score_evidence = {}
        for key in SCORE_DIMENSIONS:
            model_evidence = model_step["scoreEvidence"][key]
            capped = rule_step["scores"][key] < model_step["scores"][key]
            score_evidence[key] = {
                "evidence": list(dict.fromkeys(
                    [*model_evidence["evidence"],
                     *(rule_step["scoreEvidence"][key]["evidence"] if capped else []),
                     *_cap_evidence(node_caps.get(key, []))])),
                "reason": (model_evidence["reason"] +
                           ("；确定性规则施加保守上限：" +
                            rule_step["scoreEvidence"][key]["reason"] if capped else "") +
                           ("；确定性 oracle fail 将该维度封顶为 0：" +
                            ",".join(check["criterion"] for check in node_caps[key])
                            if key in node_caps else "")),
            }
        overall = min(sum(scores.values()) / len(scores), _cap(rule_step["findings"]))
        result = {
            "nodeId": rule_step["nodeId"], "scores": scores,
            "scoreEvidence": score_evidence,
            "overall": round(overall, 4), "label": label_for(overall),
            "confidence": "uncalibrated-judge-score",
            "findings": rule_step["findings"],
            "claims": model_step.get("claims", {}),
            "reason": model_step["reason"], "source": "hybrid",
            "grader": {"name": "conservative-hybrid", "version": "1"},
        }
        validate_step_evaluation(result)
        steps.append(result)

    values = [step["overall"] for step in steps]
    judge_values = [step["overall"] for step in judge["steps"]]
    legacy = score_trace(trace)
    # 非概率的单调变换；四位小数舍入后可能出现并列。
    deterministic_ordering = round(1 / (1 + legacy["penalty"] / 100), 4)
    judge_ordering = round(sum(judge_values) / len(judge_values), 4) if judge_values else None
    trace_cap = _cap(deterministic["traceFindings"])
    bottom = sorted(values)[:min(3, len(values))]
    trajectory = {**judge["trajectory"],
                  "trajectoryEvidence": {
                      key: {**value, "evidence": list(value["evidence"]),
                            "reason": value["reason"]}
                      for key, value in judge["trajectory"]["trajectoryEvidence"].items()}}
    for dimension, checks in oracle_caps["trajectory"].items():
        trajectory[dimension] = 0.0
        evidence = trajectory["trajectoryEvidence"][dimension]
        evidence["evidence"] = list(dict.fromkeys(
            [*evidence["evidence"], *_cap_evidence(checks)]))
        evidence["reason"] += ("；确定性 oracle fail 将该维度封顶为 0：" +
                               ",".join(check["criterion"] for check in checks))
    trajectory_keys = ("completeness", "necessity", "ordering", "evidence_closure")
    trajectory["overall"] = round(sum(trajectory[key] for key in trajectory_keys) / 4, 4)
    oracle_ordering = ((oracle or {}).get("summary") or {}).get(
        "evidenceAdjustedOrderingScore")
    aggregate_inputs = [deterministic_ordering, trace_cap, trajectory["overall"]]
    if judge_ordering is not None:
        aggregate_inputs.append(judge_ordering)
    if oracle_ordering is not None:
        aggregate_inputs.append(oracle_ordering)
    # 固定非负输入轴时单调不增；零因子饱和或舍入会产生并列，并非普遍严格下降。
    # 输入不是概率、也未做独立性假设，因此乘积仅用于排序，不是成功概率。
    hybrid = math.prod(aggregate_inputs) if aggregate_inputs else None

    return {
        "name": deterministic["name"],
        "steps": steps,
        "trajectory": trajectory,
        "oracleCaps": oracle_caps,
        "traceFindings": deterministic["traceFindings"],
        "aggregate": {
            "deterministicOrderingScore": deterministic_ordering,
            "judgeOrderingScore": judge_ordering,
            "trajectoryOrderingScore": trajectory["overall"],
            "oracleEvidenceAdjustedOrderingScore": oracle_ordering,
            "hybridOrderingScore": round(hybrid, 4) if hybrid is not None else None,
            "minimum": min(values) if values else None,
            "bottomKMean": round(sum(bottom) / len(bottom), 4) if bottom else None,
            "confidence": "uncalibrated-judge-score",
            "aggregation": "monotonic-conjunctive-product",
            "interpretation": "ordering-only-not-a-probability",
        },
        "provenance": judge.get("provenance") or {},
    }
