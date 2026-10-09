"""把确定性 oracle fail 映射为评分维度硬上限；unknown 不冒充失败。"""

from __future__ import annotations

STEP_CAP_DIMENSIONS = {
    "action.type": ("action_correctness",),
    "action.completed": ("action_correctness",),
    "target.resolved": ("action_correctness", "argument_quality"),
    "target.unique": ("action_correctness", "argument_quality"),
    "target.hit_point": ("action_correctness", "argument_quality"),
    "target.hit_margin": ("argument_quality", "replay_safety"),
    "target.semantic_identity": ("action_correctness", "argument_quality"),
    "target.visible": ("action_correctness",),
    "target.enabled": ("action_correctness",),
    "target.unobscured": ("action_correctness",),
    "reaction.network": ("evidence_quality",),
    "reaction.causal_attribution": ("context_fit", "evidence_quality"),
    "reaction.assertion": ("evidence_quality",),
    "reaction.next_step_ready": ("context_fit",),
    "reaction.downstream_assertion": ("context_fit", "evidence_quality"),
    "reaction.persistent_state": ("evidence_quality", "replay_safety"),
    "flow.required_step": ("action_correctness", "context_fit"),
    "flow.step_semantics": ("action_correctness", "argument_quality", "context_fit"),
    "flow.extra_step": ("action_correctness", "context_fit"),
}

TRAJECTORY_CAP_DIMENSIONS = {
    "flow.required_steps": ("completeness",),
    "flow.forbidden_steps": ("necessity",),
    "flow.order_constraints": ("ordering",),
    "flow.argument_conformance": ("completeness",),
    "flow.path_efficiency": ("necessity",),
    "flow.retry_budget": ("necessity",),
    "flow.duration_budget": ("necessity",),
    "flow.reaction_coverage": ("evidence_closure",),
    "flow.causal_coherence": ("evidence_closure",),
    "flow.final_outcome": ("evidence_closure",),
}


def oracle_score_caps(oracle: dict | None) -> dict:
    """返回由明确 fail 触发的 0 上限；不对 pass/unknown/not_applicable 推断。"""
    step_caps: dict[str, dict[str, list[dict]]] = {}
    trajectory_caps: dict[str, list[dict]] = {}
    if not oracle:
        return {"steps": step_caps, "trajectory": trajectory_caps,
                "policy": "definitive-fail-only"}

    for step in oracle.get("steps", []):
        node_id = step.get("actualNodeId") or step.get("referenceNodeId")
        if not node_id:
            continue
        for check in step.get("checks", []):
            if check.get("verdict") != "fail":
                continue
            for dimension in STEP_CAP_DIMENSIONS.get(check.get("criterion"), ()):
                step_caps.setdefault(node_id, {}).setdefault(dimension, []).append(check)

    for check in oracle.get("flowChecks", []):
        if check.get("verdict") != "fail":
            continue
        for dimension in TRAJECTORY_CAP_DIMENSIONS.get(check.get("criterion"), ()):
            trajectory_caps.setdefault(dimension, []).append(check)
    return {"steps": step_caps, "trajectory": trajectory_caps,
            "policy": "definitive-fail-only"}
