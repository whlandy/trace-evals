"""逐步/轨迹评分的稳定输出契约。"""

from __future__ import annotations

from typing import Any

SCORE_DIMENSIONS = (
    "action_correctness",
    "argument_quality",
    "context_fit",
    "evidence_quality",
    "replay_safety",
)

LABELS = {"pass", "weak", "risky", "fail", "unknown"}
CONFIDENCE_LEVELS = {
    "deterministic",
    "uncalibrated-judge-score",
    "calibrated",
}


def label_for(score: float) -> str:
    if score >= 0.85:
        return "pass"
    if score >= 0.65:
        return "weak"
    if score >= 0.4:
        return "risky"
    return "fail"


def validate_step_evaluation(value: dict[str, Any]) -> None:
    """Fail fast，避免 provider 或聚合器悄悄改变评分形状。"""
    missing = set(SCORE_DIMENSIONS) - set(value.get("scores") or {})
    if missing:
        raise ValueError(f"step evaluation 缺评分维度：{sorted(missing)}")
    for key in (*SCORE_DIMENSIONS,):
        score = value["scores"][key]
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
            raise ValueError(f"{key} 必须在 [0, 1]，实际为 {score!r}")
    score_evidence = value.get("scoreEvidence")
    if not isinstance(score_evidence, dict) or set(score_evidence) != set(SCORE_DIMENSIONS):
        raise ValueError("scoreEvidence 必须覆盖全部五个评分维度")
    for key in SCORE_DIMENSIONS:
        item = score_evidence[key]
        if (not isinstance(item, dict) or set(item) != {"evidence", "reason"}
                or not isinstance(item["evidence"], list) or not item["evidence"]
                or not all(isinstance(token, str) and token.strip() for token in item["evidence"])
                or not isinstance(item["reason"], str) or not item["reason"].strip()):
            raise ValueError(f"scoreEvidence.{key} 必须含非空 evidence/reason")
    overall = value.get("overall")
    if isinstance(overall, bool) or not isinstance(overall, (int, float)) or not 0 <= overall <= 1:
        raise ValueError(f"overall 必须在 [0, 1]，实际为 {overall!r}")
    if value.get("label") not in LABELS:
        raise ValueError(f"未知 label：{value.get('label')!r}")
    if value.get("confidence") not in CONFIDENCE_LEVELS:
        raise ValueError(f"未知 confidence：{value.get('confidence')!r}")
