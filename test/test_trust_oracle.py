import copy

from test_trust_mutate import _good_trace
from trust.oracle import evaluate_test_case


def _observations(trace):
    return {
        node_id: {"target": {"resolved": True, "matchCount": 1,
                             "hitWithinTarget": True,
                             "resolutionEvidenceSource": "test-fixture",
                             "resolutionEvidence": ["synthetic:target-resolved"],
                             "matchEvidenceSource": "test-fixture",
                             "matchEvidence": ["synthetic:match-count=1"],
                             "hitEvidence": "test-fixture", "hitInference": "measured"},
                  "action": {"type": (node.get("action") or {}).get("type"),
                             "completed": True, "evidenceSource": "test-fixture",
                             "evidence": ["synthetic:action-completed"]},
                  "reactions": {"network": [], "assertions": []}}
        for node_id, node in trace.items() if node_id != "$meta"
    }


def test_missing_runtime_evidence_is_unknown_not_a_middle_score():
    trace = _good_trace()
    result = evaluate_test_case(trace, trace)
    step = result["steps"][0]
    target = next(item for item in step["checks"] if item["criterion"] == "target.resolved")
    assert target["verdict"] == "unknown"
    assert target["score"] is None
    assert result["summary"]["evidenceCoverage"] < 1


def test_click_hit_and_completed_action_are_separate_checks():
    trace = _good_trace()
    observations = _observations(trace)
    observations["step_0001"]["target"]["hitWithinTarget"] = False
    result = evaluate_test_case(trace, trace, observations=observations)
    checks = {item["criterion"]: item for item in result["steps"][0]["checks"]}
    assert checks["target.hit_point"]["verdict"] == "fail"
    assert checks["action.completed"]["verdict"] == "pass"


def test_bare_action_completed_boolean_is_unknown_without_execution_proof():
    trace = _good_trace()
    observations = _observations(trace)
    observations["step_0001"]["action"] = {"type": "Click", "completed": True}
    checks = {item["criterion"]: item for item in evaluate_test_case(
        trace, trace, observations=observations)["steps"][0]["checks"]}
    assert checks["action.type"]["verdict"] == "unknown"
    assert checks["action.completed"]["verdict"] == "unknown"


def test_missing_actionability_evidence_is_unknown_not_assumed_from_hit():
    trace = _good_trace()
    observations = _observations(trace)
    result = evaluate_test_case(trace, trace, observations=observations)
    checks = {item["criterion"]: item for item in result["steps"][0]["checks"]}
    assert checks["target.hit_point"]["verdict"] == "pass"
    assert checks["target.visible"]["verdict"] == "unknown"
    assert checks["target.enabled"]["verdict"] == "unknown"
    assert checks["target.unobscured"]["verdict"] == "unknown"
    assert checks["target.semantic_identity"]["verdict"] == "unknown"


def test_bare_target_resolution_unique_and_hit_are_unknown_without_resolver_proof():
    trace = _good_trace()
    observations = _observations(trace)
    observations["step_0001"]["target"] = {
        "resolved": True, "matchCount": 1, "hitWithinTarget": True}
    checks = {item["criterion"]: item for item in evaluate_test_case(
        trace, trace, observations=observations)["steps"][0]["checks"]}
    assert checks["target.resolved"]["verdict"] == "unknown"
    assert checks["target.unique"]["verdict"] == "unknown"
    assert checks["target.hit_point"]["verdict"] == "unknown"


def test_declared_network_expectation_requires_observed_matching_response():
    trace = _good_trace()
    observations = _observations(trace)
    # step_0003 声明 POST /api/save 和 body；没有真实响应不能通过。
    del observations["step_0003"]["reactions"]["network"]
    unknown = evaluate_test_case(trace, trace, observations=observations)
    checks = {item["criterion"]: item for item in unknown["steps"][2]["checks"]}
    assert checks["reaction.network"]["verdict"] == "unknown"
    observations["step_0003"]["reactions"]["network"] = [{
        "method": "POST", "url": "/api/save", "status": 200,
        "body": {"code": "200"},
        "validationEvidenceSource": "raw-instrumented-response",
        "validationEvidence": ["captured-response-body={code:200}"]}]
    passed = evaluate_test_case(trace, trace, observations=observations)
    checks = {item["criterion"]: item for item in passed["steps"][2]["checks"]}
    assert checks["reaction.network"]["verdict"] == "pass"
    assert checks["reaction.causal_attribution"]["verdict"] == "unknown"


def test_matching_background_request_cannot_prove_click_causality():
    trace = _good_trace()
    observations = _observations(trace)
    observations["step_0003"]["reactions"]["network"] = [{
        "method": "POST", "url": "/api/save", "status": 200,
        "body": {"code": "200"}, "causallyLinked": False,
        "causalEvidence": "background-request",
        "causalEvidenceSource": "instrumented-event-window",
        "validationEvidenceSource": "raw-instrumented-response",
        "validationEvidence": ["captured-response-body={code:200}"]}]
    result = evaluate_test_case(trace, trace, observations=observations)
    checks = {item["criterion"]: item for item in result["steps"][2]["checks"]}
    assert checks["reaction.network"]["verdict"] == "pass"
    assert checks["reaction.causal_attribution"]["verdict"] == "fail"


def test_bare_network_and_causal_booleans_are_unknown_without_provenance():
    trace = _good_trace()
    observations = _observations(trace)
    observations["step_0003"]["reactions"]["network"] = [{
        "method": "POST", "url": "/api/save", "status": 200,
        "body": {"code": "200"}, "ok": True, "causallyLinked": True}]
    checks = {item["criterion"]: item for item in evaluate_test_case(
        trace, trace, observations=observations)["steps"][2]["checks"]}
    assert checks["reaction.network"]["verdict"] == "unknown"
    assert checks["reaction.causal_attribution"]["verdict"] == "unknown"


def test_bare_assertion_boolean_is_unknown_without_verifier_evidence():
    trace = _good_trace()
    observations = _observations(trace)
    observations["step_0004"]["reactions"]["assertions"] = [{"passed": True}]
    check = next(item for item in evaluate_test_case(
        trace, trace, observations=observations)["steps"][3]["checks"]
                 if item["criterion"] == "reaction.assertion")
    assert check["verdict"] == "unknown"


def test_bare_next_step_ready_boolean_is_unknown_without_execution_sequence_proof():
    trace = _good_trace()
    observations = _observations(trace)
    observations["step_0001"]["reactions"]["nextStep"] = {
        "nodeId": "step_0002", "ready": True}
    check = next(item for item in evaluate_test_case(
        trace, trace, observations=observations)["steps"][0]["checks"]
                 if item["criterion"] == "reaction.next_step_ready")
    assert check["verdict"] == "unknown"


def test_one_response_cannot_satisfy_two_expected_occurrences():
    trace = _good_trace()
    trace["step_0003"]["attach"]["verification"]["responses"].append(
        copy.deepcopy(trace["step_0003"]["attach"]["verification"]["responses"][0]))
    observations = _observations(trace)
    observations["step_0003"]["reactions"]["network"] = [{
        "method": "POST", "url": "/api/save", "status": 200,
        "body": {"code": "200"}, "causallyLinked": True}]
    result = evaluate_test_case(trace, trace, observations=observations)
    checks = {item["criterion"]: item for item in result["steps"][2]["checks"]}
    assert checks["reaction.network"]["verdict"] == "fail"
    assert checks["reaction.causal_attribution"]["verdict"] == "fail"


def test_complete_flow_detects_extra_and_changed_test_case_action():
    reference = _good_trace()
    actual = copy.deepcopy(reference)
    actual["step_0002"]["action"]["param"]["state"] = False
    result = evaluate_test_case(actual, reference, observations=_observations(actual))
    assert next(x for x in result["flowChecks"]
                if x["criterion"] == "flow.argument_conformance")["verdict"] == "fail"


def test_flow_final_outcome_requires_runtime_oracle_evidence():
    trace = _good_trace()
    unknown = evaluate_test_case(trace, trace)
    assert next(x for x in unknown["flowChecks"]
                if x["criterion"] == "flow.final_outcome")["verdict"] == "unknown"
    observations = _observations(trace)
    observations["step_0003"]["reactions"]["network"] = [{
        "method": "POST", "url": "/api/save", "status": 200,
        "body": {"code": "200"}, "causallyLinked": True,
        "causalEvidence": "captured-inside-action-window",
        "causalEvidenceSource": "instrumented-event-window",
        "validationEvidenceSource": "raw-instrumented-response",
        "validationEvidence": ["captured-response-body={code:200}"]}]
    observations["step_0004"]["reactions"]["assertions"] = [{
        "passed": True, "evidenceSource": "test-fixture",
        "evidence": ["assertion:visible=true"]}]
    passed = evaluate_test_case(trace, trace, observations=observations)
    assert next(x for x in passed["flowChecks"]
                if x["criterion"] == "flow.final_outcome")["verdict"] == "pass"


def test_trace_without_any_outcome_oracle_does_not_claim_business_success():
    trace = _good_trace()
    trace["step_0003"]["attach"]["verification"]["responses"] = []
    del trace["step_0004"]
    trace["step_0003"]["next"] = None
    result = evaluate_test_case(trace, trace, observations=_observations(trace))
    final = next(x for x in result["flowChecks"] if x["criterion"] == "flow.final_outcome")
    assert final["verdict"] == "unknown"


def test_path_efficiency_does_not_reward_skipping_required_steps():
    reference = _good_trace()
    actual = copy.deepcopy(reference)
    actual["step_0001"]["next"] = "step_0003"
    result = evaluate_test_case(actual, reference, observations=_observations(actual))
    efficiency = next(x for x in result["flowChecks"]
                      if x["criterion"] == "flow.path_efficiency")
    assert efficiency["verdict"] == "fail"
    assert efficiency["observed"]["efficiency"] == 0.0
    assert efficiency["observed"]["missingSteps"] == 1


def test_path_efficiency_reports_extra_steps_and_optional_budgets_stay_separate():
    reference = _good_trace()
    actual = copy.deepcopy(reference)
    extra = copy.deepcopy(actual["step_0002"])
    extra["next"] = "step_0002"
    actual["step_extra"] = extra
    actual["step_0001"]["next"] = "step_extra"
    result = evaluate_test_case(actual, reference, observations=_observations(actual))
    checks = {x["criterion"]: x for x in result["flowChecks"]}
    assert checks["flow.path_efficiency"]["verdict"] == "fail"
    assert checks["flow.path_efficiency"]["observed"]["efficiency"] == 0.8
    assert checks["flow.retry_budget"]["verdict"] == "not_applicable"
    assert checks["flow.duration_budget"]["verdict"] == "not_applicable"
