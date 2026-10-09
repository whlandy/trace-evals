"""运行观测的反事实变异：检验 oracle 是否看得见误点和错误反应。"""

from __future__ import annotations

import copy

from trust.trajectory_match import canonical_steps


def complete_observations(trace: dict) -> dict[str, dict]:
    """构造声明 oracle 全部被真实观测满足的合成基线，仅用于 meta-eval。"""
    result = {}
    steps = canonical_steps(trace)
    for index, step in enumerate(steps):
        network = []
        for expected in step["responses"]:
            network.append({"method": expected.get("method"), "url": expected.get("url"),
                            "status": expected.get("expectedStatus"),
                            "body": expected.get("expectedBody"), "ok": True,
                            "validationEvidenceSource": "test-fixture",
                            "validationEvidence": ["synthetic:response-validated"],
                            "causallyLinked": True,
                            "causalEvidence": "synthetic-action-window",
                            "causalEvidenceSource": "test-fixture"})
        reactions = {"network": network}
        if step["assertion"]:
            reactions["assertions"] = [{"passed": True,
                                        "evidenceSource": "test-fixture",
                                        "evidence": ["synthetic:assertion-passed"]}]
        result[step["nodeId"]] = {
            "target": {"resolved": True, "matchCount": 1, "hitWithinTarget": True,
                       "hitMarginNormalized": 0.5,
                       "resolutionEvidenceSource": "test-fixture",
                       "resolutionEvidence": ["synthetic:target-resolved"],
                       "matchEvidenceSource": "test-fixture",
                       "matchEvidence": ["synthetic:match-count=1"],
                       "hitInference": "measured",
                       "hitEvidence": "point-in-runtime-bounding-box",
                       "visible": True, "enabled": True, "unobscured": True,
                       "semanticMatch": True, "semanticIdentity": "synthetic:expected-target",
                       "semanticEvidence": ["synthetic:semantic-target-match"],
                       "semanticEvidenceSource": "test-fixture",
                       "actionabilityEvidenceSource": "test-fixture",
                       "actionabilityEvidence": {
                           "visible": ["synthetic:visible"],
                           "enabled": ["synthetic:enabled"],
                           "unobscured": ["synthetic:unobscured"]}},
            "action": {"type": step["action"], "completed": True, "skipped": False,
                       "evidenceSource": "test-fixture",
                       "evidence": [f"synthetic:actualAction={step['action']}",
                                    "synthetic:status=success"]},
            "reactions": reactions, "evidenceSource": "synthetic-meta-eval",
        }
        if index + 1 < len(steps):
            result[step["nodeId"]]["reactions"]["nextStep"] = {
                "nodeId": steps[index + 1]["nodeId"], "status": "success", "ready": True,
                "evidenceSource": "test-fixture",
                "evidence": ["synthetic:next-step-ready"]}
            if steps[index + 1]["assertion"]:
                result[step["nodeId"]]["reactions"]["downstreamAssertion"] = {
                    "nodeId": steps[index + 1]["nodeId"], "passed": True,
                    "evidenceSource": "test-fixture",
                    "evidence": ["synthetic:downstream-assertion-passed"]}
    return result


def _pointer(trace: dict) -> str:
    return next(step["nodeId"] for step in canonical_steps(trace)
                if step["action"] in {"Click", "DoubleClick", "SetSwitch", "Check", "Uncheck"})


def _network(trace: dict) -> str:
    return next(step["nodeId"] for step in canonical_steps(trace) if step["responses"])


def mutate_observation(trace: dict, observations: dict, name: str) -> tuple[dict, str]:
    changed = copy.deepcopy(observations)
    if name == "miss_target":
        node = _pointer(trace); changed[node]["target"]["hitWithinTarget"] = False
        changed[node]["target"]["hitMarginNormalized"] = -0.1
        changed[node]["target"]["hitEvidence"] = "point-outside-runtime-bounding-box"
        return changed, f"{node} 点击点移出目标"
    if name == "edge_hit":
        node = _pointer(trace); changed[node]["target"]["hitMarginNormalized"] = 0.01
        return changed, f"{node} 点击仍在框内但距最近边缘仅 1%"
    if name == "ambiguous_target":
        node = _pointer(trace); changed[node]["target"]["matchCount"] = 2
        changed[node]["target"]["matchEvidence"] = ["synthetic:match-count=2"]
        return changed, f"{node} 实际命中两个候选"
    if name == "target_not_resolved":
        node = _pointer(trace); changed[node]["target"]["resolved"] = False
        changed[node]["target"]["resolutionEvidence"] = ["synthetic:target-resolution-failed"]
        return changed, f"{node} 未定位到目标"
    if name == "hidden_target":
        node = _pointer(trace); changed[node]["target"]["visible"] = False
        changed[node]["target"]["actionabilityEvidence"]["visible"] = ["synthetic:visible=false"]
        return changed, f"{node} 点击瞬间目标不可见"
    if name == "disabled_target":
        node = _pointer(trace); changed[node]["target"]["enabled"] = False
        changed[node]["target"]["actionabilityEvidence"]["enabled"] = ["synthetic:enabled=false"]
        return changed, f"{node} 点击瞬间目标被禁用"
    if name == "obscured_target":
        node = _pointer(trace); changed[node]["target"]["unobscured"] = False
        changed[node]["target"]["actionabilityEvidence"]["unobscured"] = [
            "synthetic:unobscured=false"]
        return changed, f"{node} 点击点被其他元素遮挡"
    if name == "wrong_semantic_target":
        node = _pointer(trace); changed[node]["target"]["semanticMatch"] = False
        changed[node]["target"]["semanticIdentity"] = "synthetic:wrong-target"
        changed[node]["target"]["semanticEvidence"] = ["synthetic:semantic-target-mismatch"]
        changed[node]["target"]["semanticEvidenceSource"] = "test-fixture"
        return changed, f"{node} 点到可交互但业务语义错误的元素"
    if name == "action_failed":
        node = _pointer(trace); changed[node]["action"]["completed"] = False
        changed[node]["action"]["evidence"] = ["synthetic:status=failed"]
        return changed, f"{node} 动作执行失败"
    if name == "missing_network_reaction":
        node = _network(trace); del changed[node]["reactions"]["network"]
        return changed, f"{node} 删除点击后的网络观测"
    if name == "wrong_network_status":
        node = _network(trace); changed[node]["reactions"]["network"][0]["ok"] = False
        changed[node]["reactions"]["network"][0]["status"] = 500
        changed[node]["reactions"]["network"][0]["validationEvidence"] = [
            "synthetic:response-status=500"]
        return changed, f"{node} 响应改成 500"
    if name == "uncorrelated_network_reaction":
        node = _network(trace)
        changed[node]["reactions"]["network"][0]["causallyLinked"] = False
        changed[node]["reactions"]["network"][0]["causalEvidence"] = "background-request"
        changed[node]["reactions"]["network"][0]["causalEvidenceSource"] = "test-fixture"
        return changed, f"{node} 匹配响应改成与点击无因果关联的后台请求"
    if name == "failed_downstream_assertion":
        node = next(step["nodeId"] for index, step in enumerate(canonical_steps(trace)[:-1])
                    if canonical_steps(trace)[index + 1]["assertion"])
        changed[node]["reactions"]["downstreamAssertion"]["passed"] = False
        changed[node]["reactions"]["downstreamAssertion"]["evidence"] = [
            "synthetic:downstream-assertion-failed"]
        return changed, f"{node} 后续业务状态断言改为失败"
    if name == "failed_assertion":
        node = next(step["nodeId"] for step in canonical_steps(trace) if step["assertion"])
        changed[node]["reactions"]["assertions"][0]["passed"] = False
        changed[node]["reactions"]["assertions"][0]["evidence"] = [
            "synthetic:assertion-failed"]
        return changed, f"{node} 断言执行失败"
    if name == "next_step_not_ready":
        node = next(step["nodeId"] for step in canonical_steps(trace)
                    if changed[step["nodeId"]]["reactions"].get("nextStep"))
        changed[node]["reactions"]["nextStep"]["ready"] = False
        changed[node]["reactions"]["nextStep"]["status"] = "failed"
        changed[node]["reactions"]["nextStep"]["evidence"] = [
            "synthetic:next-step-status=failed"]
        return changed, f"{node} 动作后未进入下一可执行状态"
    raise ValueError(f"未知 observation mutation：{name}")


OBSERVATION_MUTATIONS = (
    "miss_target", "edge_hit", "ambiguous_target", "target_not_resolved", "hidden_target",
    "disabled_target", "obscured_target", "wrong_semantic_target", "action_failed",
    "missing_network_reaction", "wrong_network_status", "uncorrelated_network_reaction",
    "failed_assertion", "failed_downstream_assertion", "next_step_not_ready",
)
