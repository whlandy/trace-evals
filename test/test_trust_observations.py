from test_trust_mutate import _good_trace
from trust.observations import observations_from_execution
from trust.oracle import evaluate_test_case


def _execution(steps):
    return {"schema": "edr.execution-trace/v1", "status": "success", "steps": steps}


def test_successful_replay_becomes_target_action_and_network_evidence():
    trace = _good_trace()
    execution = _execution([{
        "nodeId": "step_0003", "actualAction": "Click", "status": "success",
        "target": {"mode": "dom"}, "responses": [{
            "method": "POST", "url": "https://example.test/api/save",
            "status": 200, "ok": True}], "retries": 0, "durationMs": 12,
    }, {"nodeId": "step_0004", "actualAction": "DoNothing", "status": "success",
        "target": {"mode": "verifier"}, "responses": []}])
    observations = observations_from_execution(trace, execution)
    assert observations["step_0003"]["target"]["resolved"] is True
    assert observations["step_0003"]["action"]["completed"] is True
    assert observations["step_0003"]["target"]["hitWithinTarget"] is True
    result = evaluate_test_case(trace, trace, observations=observations)
    network = next(x for x in result["steps"][2]["checks"]
                   if x["criterion"] == "reaction.network")
    assert network["verdict"] == "pass"
    causal = next(x for x in result["steps"][2]["checks"]
                  if x["criterion"] == "reaction.causal_attribution")
    assert causal["verdict"] == "pass"
    next_step = next(x for x in result["steps"][2]["checks"]
                     if x["criterion"] == "reaction.next_step_ready")
    assert next_step["verdict"] == "pass"


def test_failed_click_is_not_confused_with_missing_execution_evidence():
    trace = _good_trace()
    observations = observations_from_execution(trace, _execution([{
        "nodeId": "step_0002", "actualAction": "SetSwitch", "status": "failed",
        "error": "TimeoutError: pointer intercepted", "responses": [],
    }]))
    result = evaluate_test_case(trace, trace, observations=observations)
    checks = {x["criterion"]: x for x in result["steps"][1]["checks"]}
    assert checks["target.resolved"]["verdict"] == "fail"
    assert checks["action.completed"]["verdict"] == "fail"


def test_optional_absence_is_not_scored_as_a_failed_click():
    trace = _good_trace()
    observations = observations_from_execution(trace, _execution([{
        "nodeId": "step_0001", "actualAction": "Click", "status": "skipped",
        "error": "VisualAbsent", "responses": [],
    }]))
    result = evaluate_test_case(trace, trace, observations=observations)
    checks = {x["criterion"]: x for x in result["steps"][0]["checks"]}
    assert checks["target.resolved"]["verdict"] == "not_applicable"
    assert checks["action.completed"]["verdict"] == "not_applicable"


def test_assertion_success_becomes_observed_reaction():
    trace = _good_trace()
    observations = observations_from_execution(trace, _execution([{
        "nodeId": "step_0004", "actualAction": "DoNothing", "status": "success",
        "target": {"mode": "verifier"}, "responses": [],
    }]))
    result = evaluate_test_case(trace, trace, observations=observations)
    assertion = next(x for x in result["steps"][3]["checks"]
                     if x["criterion"] == "reaction.assertion")
    assert assertion["verdict"] == "pass"


def test_following_assertion_closes_previous_action_reaction_loop():
    trace = _good_trace()
    observations = observations_from_execution(trace, _execution([{
        "nodeId": "step_0003", "actualAction": "Click", "status": "success",
        "target": {"mode": "dom"}, "responses": [{"method": "POST",
        "url": "/api/save", "status": 200, "ok": True}],
    }, {"nodeId": "step_0004", "actualAction": "Assert", "status": "success",
        "target": {"mode": "verifier"}, "responses": []}]))
    result = evaluate_test_case(trace, trace, observations=observations)
    closure = next(x for x in result["steps"][2]["checks"]
                   if x["criterion"] == "reaction.downstream_assertion")
    assert closure["verdict"] == "pass"


def test_runtime_geometry_overrides_success_heuristic_for_click_hit():
    trace = _good_trace()
    observations = observations_from_execution(trace, _execution([{
        "nodeId": "step_0002", "actualAction": "SetSwitch", "status": "success",
        "target": {"mode": "visual", "point": {"x": 90, "y": 90},
                   "boundingBox": {"x": 10, "y": 10, "width": 20, "height": 20},
                   "visible": True, "enabled": True, "unobscured": True,
                   "semanticMatch": True, "semanticIdentity": "switch:policy-enabled",
                   "semanticEvidence": ["accessibility-role=switch", "name=策略启用"],
                   "semanticEvidenceSource": "accessibility-tree",
                   "actionabilityEvidenceSource": "playwright-actionability",
                   "actionabilityEvidence": {"visible": ["isVisible=true"],
                                              "enabled": ["isEnabled=true"],
                                              "unobscured": ["receivesEvents=true"]}},
        "responses": [],
    }]))
    assert observations["step_0002"]["target"]["hitWithinTarget"] is False
    assert observations["step_0002"]["target"]["hitMarginNormalized"] < 0
    assert observations["step_0002"]["target"]["hitInference"] == "measured"
    result = evaluate_test_case(trace, trace, observations=observations)
    checks = {x["criterion"]: x for x in result["steps"][1]["checks"]}
    assert checks["target.hit_point"]["verdict"] == "fail"


def test_inside_click_near_edge_is_hit_but_not_robust_hit():
    trace = _good_trace()
    observations = observations_from_execution(trace, _execution([{
        "nodeId": "step_0002", "actualAction": "SetSwitch", "status": "success",
        "target": {"mode": "visual", "point": {"x": 10.2, "y": 20},
                   "boundingBox": {"x": 10, "y": 10, "width": 20, "height": 20},
                   "visible": True, "enabled": True, "unobscured": True,
                   "semanticMatch": True, "semanticIdentity": "switch:policy-enabled",
                   "semanticEvidence": ["accessibility-role=switch"],
                   "semanticEvidenceSource": "accessibility-tree",
                   "actionabilityEvidenceSource": "playwright-actionability",
                   "actionabilityEvidence": {"visible": ["isVisible=true"],
                                              "enabled": ["isEnabled=true"],
                                              "unobscured": ["receivesEvents=true"]}},
        "responses": [],
    }]))
    result = evaluate_test_case(trace, trace, observations=observations)
    checks = {x["criterion"]: x for x in result["steps"][1]["checks"]}
    assert checks["target.hit_point"]["verdict"] == "pass"
    assert checks["target.hit_margin"]["verdict"] == "fail"
    assert checks["target.hit_margin"]["observed"] == 0.01
    assert checks["target.semantic_identity"]["verdict"] == "pass"


def test_actionability_and_semantic_identity_are_independent_checks():
    trace = _good_trace()
    observations = observations_from_execution(trace, _execution([{
        "nodeId": "step_0002", "actualAction": "SetSwitch", "status": "success",
        "target": {"mode": "dom", "visible": True, "enabled": True,
                   "unobscured": False, "semanticMatch": False,
                   "semanticIdentity": "button:delete-policy",
                   "semanticEvidence": ["accessible-name=删除策略"],
                   "semanticEvidenceSource": "accessibility-tree",
                   "actionabilityEvidenceSource": "playwright-actionability",
                   "actionabilityEvidence": {"visible": ["isVisible=true"],
                                              "enabled": ["isEnabled=true"],
                                              "unobscured": ["receivesEvents=false"]}},
        "responses": [],
    }]))
    result = evaluate_test_case(trace, trace, observations=observations)
    checks = {x["criterion"]: x for x in result["steps"][1]["checks"]}
    assert checks["target.hit_point"]["verdict"] == "pass"
    assert checks["target.unobscured"]["verdict"] == "fail"
    assert checks["target.semantic_identity"]["verdict"] == "fail"


def test_bare_semantic_boolean_cannot_prove_correct_business_target():
    trace = _good_trace()
    observations = observations_from_execution(trace, _execution([{
        "nodeId": "step_0002", "actualAction": "SetSwitch", "status": "success",
        "target": {"mode": "dom", "visible": True, "enabled": True,
                   "unobscured": True, "semanticMatch": True}, "responses": [],
    }]))
    checks = {x["criterion"]: x for x in evaluate_test_case(
        trace, trace, observations=observations)["steps"][1]["checks"]}
    assert checks["target.semantic_identity"]["verdict"] == "unknown"
    assert checks["target.semantic_identity"]["observed"]["evidencePresent"] is False


def test_unknown_semantic_evidence_source_cannot_prove_target():
    trace = _good_trace()
    observations = observations_from_execution(trace, _execution([{
        "nodeId": "step_0002", "actualAction": "SetSwitch", "status": "success",
        "target": {"mode": "dom", "semanticMatch": True,
                   "semanticIdentity": "switch:policy-enabled",
                   "semanticEvidence": ["looks-right"],
                   "semanticEvidenceSource": "model-vibes"}, "responses": [],
    }]))
    checks = {x["criterion"]: x for x in evaluate_test_case(
        trace, trace, observations=observations)["steps"][1]["checks"]}
    assert checks["target.semantic_identity"]["verdict"] == "unknown"
    assert checks["target.semantic_identity"]["observed"]["sourceAccepted"] is False


def test_bare_actionability_booleans_cannot_prove_clickability():
    trace = _good_trace()
    observations = observations_from_execution(trace, _execution([{
        "nodeId": "step_0002", "actualAction": "SetSwitch", "status": "success",
        "target": {"mode": "dom", "visible": True, "enabled": True,
                   "unobscured": True}, "responses": [],
    }]))
    checks = {x["criterion"]: x for x in evaluate_test_case(
        trace, trace, observations=observations)["steps"][1]["checks"]}
    for criterion in ("target.visible", "target.enabled", "target.unobscured"):
        assert checks[criterion]["verdict"] == "unknown"
        assert checks[criterion]["observed"]["evidencePresent"] is False


def test_successful_non_forced_dom_click_carries_playwright_actionability_proof():
    trace = _good_trace()
    observations = observations_from_execution(trace, _execution([{
        "nodeId": "step_0002", "actualAction": "SetSwitch", "status": "success",
        "target": {"mode": "dom"}, "responses": [],
    }]))
    checks = {x["criterion"]: x for x in evaluate_test_case(
        trace, trace, observations=observations)["steps"][1]["checks"]}
    for criterion in ("target.visible", "target.enabled", "target.unobscured"):
        assert checks[criterion]["verdict"] == "pass"
        assert checks[criterion]["observed"]["evidenceSource"] == "playwright-actionability"


def test_forced_dom_click_does_not_inherit_playwright_actionability_proof():
    trace = _good_trace()
    trace["step_0002"]["action"]["param"]["force"] = True
    observations = observations_from_execution(trace, _execution([{
        "nodeId": "step_0002", "actualAction": "SetSwitch", "status": "success",
        "target": {"mode": "dom"}, "responses": [],
    }]))
    checks = {x["criterion"]: x for x in evaluate_test_case(
        trace, trace, observations=observations)["steps"][1]["checks"]}
    assert checks["target.visible"]["verdict"] == "unknown"
    assert checks["target.unobscured"]["verdict"] == "unknown"
