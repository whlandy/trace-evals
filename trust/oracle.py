"""动作—反应—流程的确定性 oracle；缺少观测时明确返回 unknown。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from trust.trajectory_match import canonical_steps, match_trajectories
from trust.oracle_spec import SCHEMA_V3, validate_oracle_spec

VERDICTS = {"pass", "fail", "unknown", "not_applicable"}
MIN_ROBUST_HIT_MARGIN = 0.05
SEMANTIC_EVIDENCE_SOURCES = {
    "accessibility-tree", "dom-attributes", "visual-human-verified", "test-fixture"
}
ACTIONABILITY_EVIDENCE_SOURCES = {
    "playwright-actionability", "accessibility-tree", "dom-geometry",
    "visual-human-verified", "test-fixture"
}
NETWORK_VALIDATION_SOURCES = {"edr-response-validator", "raw-instrumented-response", "test-fixture"}
CAUSAL_EVIDENCE_SOURCES = {
    "action-scoped-response-waiter", "instrumented-event-window", "test-fixture"
}
ASSERTION_EVIDENCE_SOURCES = {
    "playwright-assertion", "api-verifier", "database-verifier",
    "visual-human-verified", "test-fixture"
}
NEXT_STEP_EVIDENCE_SOURCES = {"edr-execution-sequence", "test-fixture"}
PERSISTENCE_EVIDENCE_SOURCES = {
    "edr-persistence-verifier", "api-verifier", "database-verifier",
    "visual-human-verified", "test-fixture"
}
ACTION_EXECUTION_EVIDENCE_SOURCES = {"edr.execution-trace/v1", "instrumented-tool", "test-fixture"}
TARGET_RESOLUTION_EVIDENCE_SOURCES = {"edr-target-resolver", "instrumented-tool", "test-fixture"}
HIT_EVIDENCE_VALUES = {
    "point-in-runtime-bounding-box", "point-outside-runtime-bounding-box",
    "playwright-actionability", "visual-match-derived-point", "test-fixture"
}


def check(criterion: str, verdict: str, *, expected=None, observed=None,
          evidence: list[str] | None = None, reason: str = "") -> dict:
    if verdict not in VERDICTS:
        raise ValueError(f"未知 verdict：{verdict}")
    return {"criterion": criterion, "verdict": verdict,
            "score": 1.0 if verdict == "pass" else 0.0 if verdict == "fail" else None,
            "expected": expected, "observed": observed,
            "evidence": evidence or [], "reason": reason}


def _known(observation: dict | None, path: tuple[str, ...]):
    value: Any = observation
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return False, None
        value = value[key]
    return True, value


def evaluate_action(step: dict, observation: dict | None) -> list[dict]:
    node_id = step["nodeId"]
    result = []
    skipped_optional = bool(step.get("optional") and
                            ((observation or {}).get("action") or {}).get("skipped"))
    known, resolved = _known(observation, ("target", "resolved"))
    resolution_source = ((observation or {}).get("target") or {}).get(
        "resolutionEvidenceSource")
    resolution_evidence = ((observation or {}).get("target") or {}).get("resolutionEvidence")
    valid_resolution_proof = (resolution_source in TARGET_RESOLUTION_EVIDENCE_SOURCES
                              and _nonempty_string_evidence(resolution_evidence))
    result.append(check("target.resolved", "not_applicable" if skipped_optional else
                        "unknown" if not known or not valid_resolution_proof else
                        "pass" if resolved else "fail",
                        expected=True, observed=resolved if known else None,
                        evidence=[f"nodeId={node_id}",
                                  *(resolution_evidence if valid_resolution_proof else [])],
                        reason="运行时是否真正定位到点击目标"))

    known, count = _known(observation, ("target", "matchCount"))
    match_source = ((observation or {}).get("target") or {}).get("matchEvidenceSource")
    match_evidence = ((observation or {}).get("target") or {}).get("matchEvidence")
    valid_match_proof = (match_source in TARGET_RESOLUTION_EVIDENCE_SOURCES
                         and _nonempty_string_evidence(match_evidence))
    result.append(check("target.unique", "not_applicable" if skipped_optional else
                        "unknown" if not known or not valid_match_proof else
                        "pass" if count == 1 else "fail",
                        expected=1, observed=count if known else None,
                        evidence=[f"nodeId={node_id}",
                                  *(match_evidence if valid_match_proof else [])],
                        reason="有受控候选计数证据的实际匹配目标数量"))

    is_pointer = step.get("action") in {"Click", "DoubleClick", "SetSwitch", "Check", "Uncheck"}
    known, inside = _known(observation, ("target", "hitWithinTarget"))
    hit_evidence = ((observation or {}).get("target") or {}).get("hitEvidence")
    hit_inference = ((observation or {}).get("target") or {}).get("hitInference")
    valid_hit_proof = (isinstance(hit_evidence, str) and hit_evidence in HIT_EVIDENCE_VALUES
                       and hit_inference in {"measured", "executor-derived"})
    verdict = ("not_applicable" if not is_pointer or skipped_optional else
               "unknown" if not known or not valid_hit_proof else
               "pass" if inside else "fail")
    result.append(check("target.hit_point", verdict, expected=True if is_pointer else None,
                        observed=inside if known else None,
                        evidence=[f"nodeId={node_id}",
                                  *([f"hitEvidence={hit_evidence}",
                                     f"hitInference={hit_inference}"] if valid_hit_proof else [])],
                        reason="有受控几何/执行器证据的点击坐标是否落在可交互目标内"))

    known, margin = _known(observation, ("target", "hitMarginNormalized"))
    margin_verdict = ("not_applicable" if not is_pointer or skipped_optional else
                      "pass" if known and isinstance(margin, (int, float))
                      and not isinstance(margin, bool) and margin >= MIN_ROBUST_HIT_MARGIN else
                      "fail" if known else "unknown")
    result.append(check(
        "target.hit_margin", margin_verdict,
        expected={"minimumNormalizedEdgeMargin": MIN_ROBUST_HIT_MARGIN}
        if is_pointer else None,
        observed=margin if known else None,
        evidence=[f"nodeId={node_id}", *( [f"normalizedEdgeMargin={margin}"] if known else [])],
        reason=("点击点距最近目标边缘的归一化裕量；不替代 point-in-box，"
                "用于识别 DPI、缩放或舍入下易漂出目标的脆弱点击")))

    # 几何命中不是可操作性的充分条件：隐藏、禁用或被遮挡的元素即使 bbox 包含坐标也不算。
    actionability_evidence = ((observation or {}).get("target") or {}).get(
        "actionabilityEvidence")
    actionability_source = ((observation or {}).get("target") or {}).get(
        "actionabilityEvidenceSource")
    valid_actionability_source = actionability_source in ACTIONABILITY_EVIDENCE_SOURCES
    for field, criterion, reason in (
        ("visible", "target.visible", "点击瞬间目标是否可见"),
        ("enabled", "target.enabled", "点击瞬间目标是否启用"),
        ("unobscured", "target.unobscured", "点击点是否未被其他元素遮挡"),
    ):
        known, value = _known(observation, ("target", field))
        field_evidence = ((actionability_evidence or {}).get(field)
                          if isinstance(actionability_evidence, dict) else None)
        valid_field_evidence = (isinstance(field_evidence, list) and bool(field_evidence)
                                and all(isinstance(item, str) and item.strip()
                                        for item in field_evidence))
        result.append(check(
            criterion,
            "not_applicable" if not is_pointer or skipped_optional else
            "unknown" if not known or not valid_actionability_source
            or not valid_field_evidence else
            "pass" if value is True else "fail",
            expected=True if is_pointer else None,
            observed={"value": value if known else None,
                      "evidenceSource": actionability_source,
                      "sourceAccepted": valid_actionability_source,
                      "evidencePresent": valid_field_evidence},
            evidence=[f"nodeId={node_id}",
                      *([f"actionabilitySource={actionability_source}"]
                        if valid_actionability_source else []),
                      *(field_evidence if valid_field_evidence else [])],
            reason=reason + "；裸布尔值没有证明力，必须提供逐字段受控来源证据"))

    semantic_known, semantic_match = _known(observation, ("target", "semanticMatch"))
    identity_known, semantic_identity = _known(observation, ("target", "semanticIdentity"))
    evidence_known, semantic_evidence = _known(observation, ("target", "semanticEvidence"))
    source_known, semantic_source = _known(observation, ("target", "semanticEvidenceSource"))
    valid_semantic_evidence = (evidence_known and isinstance(semantic_evidence, list)
                               and bool(semantic_evidence)
                               and all(isinstance(item, str) and item.strip()
                                       for item in semantic_evidence))
    valid_identity = identity_known and isinstance(semantic_identity, str) and bool(
        semantic_identity.strip())
    valid_semantic_source = source_known and semantic_source in SEMANTIC_EVIDENCE_SOURCES
    if not is_pointer or skipped_optional:
        semantic_verdict = "not_applicable"
    elif (not semantic_known or not valid_identity or not valid_semantic_evidence
          or not valid_semantic_source):
        semantic_verdict = "unknown"
    else:
        semantic_verdict = "pass" if semantic_match is True else "fail"
    result.append(check(
        "target.semantic_identity", semantic_verdict,
        expected={"selector": step.get("selector"), "businessTargetFromTestCase": True}
        if is_pointer else None,
        observed={"matches": semantic_match if semantic_known else None,
                  "identity": semantic_identity if identity_known else None,
                  "evidencePresent": valid_semantic_evidence,
                  "evidenceSource": semantic_source if source_known else None,
                  "sourceAccepted": valid_semantic_source},
        evidence=[f"nodeId={node_id}",
                  *([f"semanticSource={semantic_source}"] if valid_semantic_source else []),
                  *(semantic_evidence if valid_semantic_evidence else [])],
        reason=("运行时目标是否是测试用例要求的业务对象；裸 semanticMatch 布尔值不构成证明，"
                "必须同时给出实际身份、可审计证据和受控证据来源")))

    known, actual_type = _known(observation, ("action", "type"))
    action_source_known, action_source = _known(observation, ("action", "evidenceSource"))
    action_evidence_known, action_evidence = _known(observation, ("action", "evidence"))
    valid_action_proof = (action_source_known
                          and action_source in ACTION_EXECUTION_EVIDENCE_SOURCES
                          and action_evidence_known
                          and _nonempty_string_evidence(action_evidence))
    expected_type = step.get("action")
    result.append(check("action.type", "not_applicable" if skipped_optional else
                        "unknown" if not known or not valid_action_proof else
                        "pass" if actual_type == expected_type else "fail", expected=expected_type,
                        observed={"type": actual_type if known else None,
                                  "evidenceSource": action_source if action_source_known else None,
                                  "evidencePresent": valid_action_proof},
                        evidence=[f"nodeId={node_id}",
                                  *(action_evidence if valid_action_proof else [])],
                        reason="实际动作类型是否有受控执行证据且与测试用例一致"))

    known, completed = _known(observation, ("action", "completed"))
    result.append(check("action.completed", "not_applicable" if skipped_optional else
                        "unknown" if not known or not valid_action_proof else
                        "pass" if completed else "fail",
                        expected=True, observed={"completed": completed if known else None,
                                                "evidenceSource": (action_source
                                                                   if action_source_known else None),
                                                "evidencePresent": valid_action_proof},
                        evidence=[f"nodeId={node_id}",
                                  *(action_evidence if valid_action_proof else [])],
                        reason="浏览器/工具是否以受控执行证据确认动作完成"))
    return result


def _response_matches(expected: dict, observed: dict) -> bool:
    if expected.get("method") is not None and observed.get("method") != expected.get("method"):
        return False
    if expected.get("url") is not None:
        wanted, actual = urlsplit(expected["url"]), urlsplit(observed.get("url") or "")
        if observed.get("url") != expected["url"] and (actual.path, actual.query) != (wanted.path, wanted.query):
            return False
    status = expected.get("expectedStatus")
    if status is not None and observed.get("status") != status:
        return False
    # replay_trace 的 response.ok 只有在 status/request body/response body 全部通过
    # _validate_response 后才会变 True；原始 body 为避免泄密不会写进 execution。
    body = expected.get("expectedBody")
    if body is not None and not observed.get("ok") and observed.get("body") != body:
        return False
    return True


def _matched_responses(expected_responses: list[dict], observed_responses: list[dict]) -> list[dict]:
    """一对一消费匹配；同一个实际响应不能证明两个预期请求。"""
    remaining = list(observed_responses)
    matched = []
    for expected in expected_responses:
        if not expected.get("required", True):
            continue
        index = next((i for i, actual in enumerate(remaining)
                      if _response_matches(expected, actual)), None)
        if index is not None:
            matched.append(remaining.pop(index))
    return matched


def _nonempty_string_evidence(value) -> bool:
    return (isinstance(value, list) and bool(value)
            and all(isinstance(item, str) and item.strip() for item in value))


def _valid_network_proof(item: dict) -> bool:
    return (item.get("validationEvidenceSource") in NETWORK_VALIDATION_SOURCES
            and _nonempty_string_evidence(item.get("validationEvidence")))


def _valid_causal_proof(item: dict) -> bool:
    return (item.get("causalEvidenceSource") in CAUSAL_EVIDENCE_SOURCES
            and isinstance(item.get("causalEvidence"), str)
            and bool(item["causalEvidence"].strip()))


def _valid_assertion_proof(item: dict) -> bool:
    return (item.get("evidenceSource") in ASSERTION_EVIDENCE_SOURCES
            and _nonempty_string_evidence(item.get("evidence")))


def evaluate_reaction(step: dict, observation: dict | None,
                      persistence_spec: dict | None = None) -> list[dict]:
    node_id = step["nodeId"]
    expected_responses = step.get("responses") or []
    observed_responses = ((observation or {}).get("reactions") or {}).get("network")
    if not expected_responses:
        network = check("reaction.network", "not_applicable", expected=[], observed=observed_responses,
                        evidence=[f"nodeId={node_id}"], reason="测试用例未声明网络 oracle")
    elif observed_responses is None:
        network = check("reaction.network", "unknown", expected=expected_responses, observed=None,
                        evidence=[f"nodeId={node_id}"], reason="声明了预期请求，但没有运行观测")
    else:
        required_count = sum(item.get("required", True) for item in expected_responses)
        matched = _matched_responses(expected_responses, observed_responses)
        verdict = ("fail" if len(matched) != required_count else
                   "pass" if all(_valid_network_proof(item) for item in matched) else "unknown")
        proof = [token for item in matched for token in item.get("validationEvidence", [])
                 if _valid_network_proof(item)]
        network = check("reaction.network", verdict,
                        expected=expected_responses, observed=observed_responses,
                        evidence=[f"nodeId={node_id}", *proof],
                        reason="必需网络响应是否真实出现、匹配且带受控校验证据")

    if not expected_responses:
        causal = check("reaction.causal_attribution", "not_applicable", expected=None,
                       observed=None, evidence=[f"nodeId={node_id}"],
                       reason="测试用例未声明需要归因到该动作的网络反应")
    elif observed_responses is None:
        causal = check("reaction.causal_attribution", "unknown", expected="action-caused",
                       observed=None, evidence=[f"nodeId={node_id}"],
                       reason="没有网络反应观测，无法判断因果归属")
    else:
        required_count = sum(item.get("required", True) for item in expected_responses)
        matched = _matched_responses(expected_responses, observed_responses)
        if len(matched) < required_count:
            verdict = "fail"
        elif not all(_valid_causal_proof(item) for item in matched):
            verdict = "unknown"
        elif all(item.get("causallyLinked") is True for item in matched):
            verdict = "pass"
        elif any("causallyLinked" in item for item in matched):
            verdict = "fail"
        else:
            verdict = "unknown"
        causal = check("reaction.causal_attribution", verdict, expected="action-caused",
                       observed=[item.get("causalEvidence") for item in matched],
                       evidence=[f"nodeId={node_id}",
                                 *[item["causalEvidence"] for item in matched
                                   if _valid_causal_proof(item)]],
                       reason="匹配响应是否由动作前布置的监听器捕获，而非无关后台请求")

    expected_assertion = step.get("assertion") or {}
    observed_assertions = ((observation or {}).get("reactions") or {}).get("assertions")
    if not expected_assertion:
        assertion = check("reaction.assertion", "not_applicable", expected={},
                          observed=observed_assertions, evidence=[f"nodeId={node_id}"],
                          reason="该步骤未声明 UI/状态断言")
    elif observed_assertions is None:
        assertion = check("reaction.assertion", "unknown", expected=expected_assertion,
                          observed=None, evidence=[f"nodeId={node_id}"],
                          reason="声明了断言，但没有执行结果")
    else:
        proven = [item for item in observed_assertions if _valid_assertion_proof(item)]
        verdict = ("unknown" if observed_assertions and not proven else
                   "pass" if any(item.get("passed") is True for item in proven) else "fail")
        assertion = check("reaction.assertion", verdict,
                          expected=expected_assertion, observed=observed_assertions,
                          evidence=[f"nodeId={node_id}",
                                    *[token for item in proven for token in item["evidence"]]],
                          reason="断言是否在运行时通过且带受控 verifier 证据")
    next_expected = step.get("nextExpectedNodeId")
    next_observed = ((observation or {}).get("reactions") or {}).get("nextStep")
    if next_expected is None:
        next_step = check("reaction.next_step_ready", "not_applicable", expected=None,
                          observed=next_observed, evidence=[f"nodeId={node_id}"],
                          reason="测试用例在此结束")
    elif next_observed is None:
        next_step = check("reaction.next_step_ready", "unknown", expected=next_expected,
                          observed=None, evidence=[f"nodeId={node_id}"],
                          reason="没有下一测试步骤的运行证据")
    else:
        proven = (next_observed.get("evidenceSource") in NEXT_STEP_EVIDENCE_SOURCES
                  and _nonempty_string_evidence(next_observed.get("evidence")))
        passed = next_observed.get("nodeId") == next_expected and next_observed.get("ready") is True
        next_step = check("reaction.next_step_ready", "unknown" if not proven else
                          "pass" if passed else "fail",
                          expected=next_expected, observed=next_observed,
                          evidence=[f"nodeId={node_id}",
                                    *(next_observed.get("evidence") if proven else [])],
                          reason="动作后是否以受控执行序列证据进入测试用例要求的下一可执行状态")

    next_assertion = step.get("nextExpectedAssertion") or {}
    downstream = ((observation or {}).get("reactions") or {}).get("downstreamAssertion")
    if not next_assertion:
        closure = check("reaction.downstream_assertion", "not_applicable", expected={},
                        observed=downstream, evidence=[f"nodeId={node_id}"],
                        reason="下一步骤不是业务状态断言")
    elif downstream is None:
        closure = check("reaction.downstream_assertion", "unknown", expected=next_assertion,
                        observed=None, evidence=[f"nodeId={node_id}"],
                        reason="后续断言存在，但没有与本动作连接的运行证据")
    else:
        proven = _valid_assertion_proof(downstream)
        verdict = ("unknown" if not proven else
                   "pass" if downstream.get("passed") is True
                   and downstream.get("nodeId") == next_expected else "fail")
        closure = check("reaction.downstream_assertion", verdict,
                        expected=next_assertion, observed=downstream,
                        evidence=[f"nodeId={node_id}", f"verifier={downstream.get('nodeId')}",
                                  *(downstream.get("evidence") if proven else [])],
                        reason="紧随动作的测试断言是否验证了预期业务后置状态")
    persistence_observed = ((observation or {}).get("reactions") or {}).get("persistence")
    if persistence_spec is None:
        persistence = check("reaction.persistent_state", "not_applicable", expected=None,
                            observed=persistence_observed, evidence=[f"nodeId={node_id}"],
                            reason="测试用例未要求刷新/重查后的持久状态复验")
    elif persistence_observed is None:
        persistence = check("reaction.persistent_state", "unknown",
                            expected=persistence_spec, observed=None,
                            evidence=[f"nodeId={node_id}"],
                            reason="测试用例要求持久状态复验，但没有运行观测")
    else:
        evidence = persistence_observed.get("evidence") or []
        valid_evidence = _nonempty_string_evidence(evidence)
        valid_source = persistence_observed.get("evidenceSource") in PERSISTENCE_EVIDENCE_SOURCES
        delay_ok = (persistence_spec.get("maxDelayMs") is None
                    or (isinstance(persistence_observed.get("delayMs"), (int, float))
                        and persistence_observed["delayMs"] <= persistence_spec["maxDelayMs"]))
        passed = (persistence_observed.get("passed") is True
                  and persistence_observed.get("method") == persistence_spec["method"]
                  and persistence_observed.get("observed") == persistence_spec["expected"]
                  and delay_ok)
        verdict = "unknown" if not valid_evidence or not valid_source else "pass" if passed else "fail"
        persistence = check("reaction.persistent_state", verdict,
                            expected=persistence_spec, observed=persistence_observed,
                            evidence=[f"nodeId={node_id}",
                                      *([f"persistenceSource={persistence_observed.get('evidenceSource')}"]
                                        if valid_source else []),
                                      *(evidence if valid_evidence else [])],
                            reason="刷新、重新查询或新会话后业务状态是否有受控 verifier 证据并满足测试用例")
    return [network, causal, assertion, next_step, closure, persistence]


def summarize(checks: list[dict]) -> dict:
    scored = [item["score"] for item in checks if item["score"] is not None]
    applicable = [item for item in checks if item["verdict"] != "not_applicable"]
    known = [item for item in applicable if item["verdict"] != "unknown"]
    ordering = round(sum(scored) / len(scored), 4) if scored else None
    coverage = round(len(known) / len(applicable), 4) if applicable else 1.0
    return {
        "orderingScore": ordering,
        "evidenceCoverage": coverage,
        # 用覆盖率门控排序值：删掉失败证据、把 fail 变成 unknown 不能抬分。
        # 仍然只是排序量，不是成功概率。
        "evidenceAdjustedOrderingScore": (
            round(ordering * coverage, 4) if ordering is not None else None),
        "passed": sum(item["verdict"] == "pass" for item in checks),
        "failed": sum(item["verdict"] == "fail" for item in checks),
        "unknown": sum(item["verdict"] == "unknown" for item in checks),
        "interpretation": "ordering-only-not-a-probability",
    }


def evaluate_test_case(actual: dict, reference: dict, *, observations: dict[str, dict] | None = None,
                       oracle_spec: dict | None = None) -> dict:
    observations = observations or {}
    actual_steps = canonical_steps(actual)
    reference_steps = canonical_steps(reference)
    oracle_spec = validate_oracle_spec(oracle_spec,
                                      node_ids={step["nodeId"] for step in reference_steps})
    flow_spec = oracle_spec.get("flow") or {}
    flexible_flow = oracle_spec["schema"] == SCHEMA_V3
    optional_ids = ({step["nodeId"] for step in reference_steps if step.get("optional")}
                    | set(flow_spec.get("optionalNodeIds", []))) if flexible_flow else set()
    actual_by_id = {step["nodeId"]: step for step in actual_steps}
    expected_by_id = {step["nodeId"]: step for step in reference_steps}
    actual_successors = {step["nodeId"]: successor
                         for step, successor in zip(actual_steps, actual_steps[1:])}
    strict = match_trajectories(actual, reference, "strict")
    allowed_extra_actions = set(flow_spec.get("allowedExtraActions", []))
    extra_steps = ([step for step in actual_steps if step["nodeId"] not in expected_by_id]
                   if flexible_flow else strict["extra"])
    disallowed_extra = [step for step in extra_steps
                        if not flexible_flow or step["action"] not in allowed_extra_actions]
    disallowed_ids = {step["nodeId"] for step in disallowed_extra}
    extra_ids = {step["nodeId"] for step in extra_steps}

    def with_successor(expected: dict, successor: dict | None) -> dict:
        return {**expected,
                "nextExpectedNodeId": successor["nodeId"] if successor else None,
                "nextExpectedAssertion": (successor.get("assertion") or {}) if successor else {}}

    steps = []
    for index, expected in enumerate(reference_steps):
        actual_step = (actual_by_id.get(expected["nodeId"]) if flexible_flow else
                       actual_steps[index] if index < len(actual_steps) else None)
        if flexible_flow:
            # Readiness verifies the executed path, while flow constraints independently
            # decide whether that path is legal. Assertion expectations remain reference-owned.
            successor = actual_successors.get(expected["nodeId"])
            successor = (expected_by_id.get(successor["nodeId"],
                         {"nodeId": successor["nodeId"]}) if successor else None)
        else:
            successor = reference_steps[index + 1] if index + 1 < len(reference_steps) else None
        expected = with_successor(expected, successor)
        if actual_step is None:
            if expected["nodeId"] in optional_ids:
                checks = [check("flow.optional_step", "not_applicable", expected=expected, observed=None,
                                evidence=[f"referenceIndex={index}"],
                                reason="测试用例明确允许该条件步骤不执行")]
            else:
                checks = [check("flow.required_step", "fail", expected=expected, observed=None,
                                evidence=[f"referenceIndex={index}"], reason="缺少测试用例必需步骤")]
        else:
            checks = evaluate_action(expected, observations.get(actual_step["nodeId"]))
            checks += evaluate_reaction(
                expected, observations.get(actual_step["nodeId"]),
                (oracle_spec["nodes"].get(expected["nodeId"]) or {}).get("persistence"))
            checks.append(check("flow.step_semantics", "pass" if {
                k: actual_step[k] for k in ("action", "arguments", "selector")
            } == {k: expected[k] for k in ("action", "arguments", "selector")} else "fail",
                expected=expected, observed=actual_step,
                evidence=[f"actualNodeId={actual_step['nodeId']}", f"referenceIndex={index}"],
                reason="动作、参数与目标是否符合测试用例"))
        steps.append({"referenceNodeId": expected["nodeId"],
                      "actualNodeId": actual_step["nodeId"] if actual_step else None,
                      "checks": checks, "summary": summarize(checks)})

    # Every executed node has exactly one Oracle record, including auxiliary actions.
    matched_ids = {step["actualNodeId"] for step in steps}
    for actual_step in actual_steps:
        if actual_step["nodeId"] in matched_ids:
            continue
        successor = actual_successors.get(actual_step["nodeId"])
        successor = (expected_by_id.get(successor["nodeId"],
                     {"nodeId": successor["nodeId"]}) if successor else None)
        runtime_step = with_successor(actual_step, successor)
        checks = evaluate_action(runtime_step, observations.get(actual_step["nodeId"]))
        checks += evaluate_reaction(runtime_step, observations.get(actual_step["nodeId"]))
        if not flexible_flow and actual_step["nodeId"] not in extra_ids:
            checks.append(check(
                "flow.step_semantics", "fail", expected=None, observed=actual_step,
                evidence=[f"actualNodeId={actual_step['nodeId']}"],
                reason="严格逐位置匹配中，该执行位置没有对应参考步骤"))
        steps.append({"referenceNodeId": None, "actualNodeId": actual_step["nodeId"],
                      "checks": checks})
    for step in steps:
        node_id = step["actualNodeId"]
        if node_id in extra_ids:
            allowed = node_id not in disallowed_ids
            step["checks"].append(check(
                "flow.allowed_extra_step" if allowed else "flow.extra_step",
                "pass" if allowed else "fail",
                expected="allowed auxiliary action" if allowed else None,
                observed=actual_by_id[node_id], evidence=[f"actualNodeId={node_id}"],
                reason="测试用例允许该辅助动作，但仍须检查运行证据" if allowed else
                       "实际轨迹包含测试用例未允许的额外步骤"))
        step["summary"] = summarize(step["checks"])
    step_checks = [item for step in steps for item in step["checks"]]
    # Extra actions cannot invent business requirements or prove the reference outcome.
    reference_checks = [item for step in steps if step["referenceNodeId"] is not None
                        for item in step["checks"]]
    outcome_checks = [item for item in reference_checks
                      if item["criterion"] in {"reaction.network", "reaction.assertion",
                                               "reaction.persistent_state",
                                               "reaction.downstream_assertion"}
                      and item["verdict"] != "not_applicable"]
    # downstream_assertion 是动作→断言的链路证据；最终 outcome 本身由直接网络/UI/状态
    # oracle 决定，避免同一断言因缺少链路元数据被重复降级。
    final_outcome_checks = [item for item in outcome_checks
                            if item["criterion"] != "reaction.downstream_assertion"]
    causal_checks = [item for item in step_checks
                     if item["criterion"] == "reaction.causal_attribution"
                     and item["verdict"] != "not_applicable"]

    def combined_verdict(items: list[dict], *, absent: str = "unknown") -> str:
        if not items:
            return absent
        if any(item["verdict"] == "fail" for item in items):
            return "fail"
        if any(item["verdict"] == "unknown" for item in items):
            return "unknown"
        return "pass"

    if flexible_flow:
        reference_ids = [step["nodeId"] for step in reference_steps]
        actual_ids = [step["nodeId"] for step in actual_steps]
        required_ids = [node_id for node_id in reference_ids if node_id not in optional_ids]
        missing_ids = [node_id for node_id in required_ids if node_id not in actual_by_id]
        explicit_order = flow_spec.get("orderConstraints")
        if explicit_order is not None:
            order_constraints = explicit_order
        else:
            present_reference = [node_id for node_id in reference_ids if node_id in actual_by_id]
            order_constraints = [{"before": left, "after": right}
                                 for left, right in zip(present_reference, present_reference[1:])]
        positions = {node_id: index for index, node_id in enumerate(actual_ids)}
        order_violations = [item for item in order_constraints
                            if item["before"] in positions and item["after"] in positions
                            and positions[item["before"]] >= positions[item["after"]]]
        argument_mismatches = []
        for node_id in reference_ids:
            if node_id not in actual_by_id:
                continue
            actual_value, expected_value = actual_by_id[node_id], expected_by_id[node_id]
            keys = ("action", "arguments", "selector")
            if any(actual_value[key] != expected_value[key] for key in keys):
                argument_mismatches.append({"nodeId": node_id, "actual": actual_value,
                                            "reference": expected_value})
    else:
        missing_ids = [step["nodeId"] for step in strict["missing"]]
        disallowed_extra = strict["extra"]
        order_violations = ([{"expected": "reference order", "observed": "different"}]
                            if strict["orderMismatch"] else [])
        argument_mismatches = strict["argumentMismatches"]
        required_ids = [step["nodeId"] for step in reference_steps]
    missing_required = bool(missing_ids)
    effective_actual_count = len(required_ids) + len(disallowed_extra)
    path_efficiency = (0.0 if missing_required else
                       round(len(required_ids) / max(effective_actual_count, 1), 4))
    max_extra = flow_spec.get("maxExtraSteps", 0)
    path_budget_passed = not missing_required and len(disallowed_extra) <= max_extra

    def observed_total(field: str) -> tuple[bool, float | None]:
        values = []
        for step in actual_steps:
            known, value = _known(observations.get(step["nodeId"]), ("action", field))
            if not known or isinstance(value, bool) or not isinstance(value, (int, float)):
                return False, None
            values.append(float(value))
        return True, sum(values)

    retries_known, total_retries = observed_total("retries")
    duration_known, total_duration = observed_total("durationMs")

    def budget_check(criterion: str, budget_key: str, known: bool,
                     observed: float | None, reason: str) -> dict:
        budget = flow_spec.get(budget_key)
        if budget is None:
            return check(criterion, "not_applicable", expected=None,
                         observed=observed if known else None, reason=reason + "；测试用例未声明预算")
        return check(criterion, "pass" if known and observed <= budget else
                     "fail" if known else "unknown", expected={"maximum": budget},
                     observed=observed if known else None, reason=reason)

    flow_checks = [
        check("flow.required_steps", "pass" if not missing_ids else "fail",
              expected=required_ids, observed=sorted(set(actual_by_id) & set(required_ids)),
              reason="测试用例必需步骤是否完整；可选步骤缺失不计失败"),
        check("flow.forbidden_steps", "pass" if not disallowed_extra else "fail",
              expected={"allowedExtraActions": sorted(flow_spec.get("allowedExtraActions", []))},
              observed=disallowed_extra, reason="是否出现测试用例未允许的额外动作"),
        check("flow.order_constraints", "pass" if not order_violations else "fail",
              expected=order_constraints if flexible_flow else "reference order",
              observed=order_violations if order_violations else "satisfied",
              reason="有依赖的步骤顺序是否一致"),
        check("flow.argument_conformance", "pass" if not argument_mismatches else "fail",
              expected="reference arguments", observed=argument_mismatches,
              reason="逐步参数与目标是否一致"),
        check("flow.path_efficiency", "pass" if path_budget_passed else "fail",
              expected={"maxExtraSteps": max_extra, "qualityCondition": "no missing required step"},
              observed={"efficiency": path_efficiency, "actualSteps": len(actual_steps),
                        "referenceSteps": len(reference_steps),
                        "extraSteps": len(disallowed_extra), "missingSteps": len(missing_ids)},
              reason="质量条件化路径效率；缺必需步骤时效率为 0，不能靠少做步骤获益"),
        budget_check("flow.retry_budget", "maxTotalRetries", retries_known, total_retries,
                     "整条轨迹运行时重试次数是否在测试用例预算内"),
        budget_check("flow.duration_budget", "maxDurationMs", duration_known, total_duration,
                     "整条轨迹运行耗时是否在测试用例预算内"),
        check("flow.reaction_coverage", combined_verdict(outcome_checks),
              expected="all declared outcome oracles observed",
              observed={item["criterion"]: item["verdict"] for item in outcome_checks},
              reason="所有关键动作是否具备可判定的反应/后置状态证据"),
        check("flow.causal_coherence", combined_verdict(causal_checks, absent="not_applicable"),
              expected="action-attributed reactions",
              observed={item["criterion"]: item["verdict"] for item in causal_checks},
              reason="声明的网络副作用是否能归因到对应动作"),
        check("flow.final_outcome", combined_verdict(final_outcome_checks),
              expected="declared business outcome oracles pass",
              observed={item["criterion"]: item["verdict"] for item in final_outcome_checks},
              reason=("最终业务结果必须由网络、UI 或状态断言证明；没有 outcome oracle 时"
                      "保持 unknown，而不是把流程走完当成成功")),
    ]
    all_checks = step_checks + flow_checks
    extra_step_policy = {
        "allowed": [step["nodeId"] for step in extra_steps
                    if step["nodeId"] not in disallowed_ids],
        "disallowed": [step["nodeId"] for step in disallowed_extra],
    }
    required_checks = [item for step in steps if step["referenceNodeId"] in required_ids
                       for item in step["checks"]]
    optional_steps = [step for step in steps if step["referenceNodeId"] in optional_ids]
    return {"steps": steps, "flowChecks": flow_checks, "summary": summarize(all_checks),
            "requiredStepSummary": summarize(required_checks),
            "optionalExecution": {"declared": len(optional_ids),
                                  "executed": sum(step["actualNodeId"] is not None
                                                  for step in optional_steps),
                                  "omitted": sum(step["actualNodeId"] is None
                                                 for step in optional_steps)},
            "match": strict, "flowPolicy": "constraint" if flexible_flow else "strict",
            "extraStepPolicy": extra_step_policy,
            "confidence": "deterministic-with-observation-coverage"}
