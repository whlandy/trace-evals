import copy

import pytest

from test_trust_mutate import _good_trace
from trust.oracle import evaluate_test_case
from trust.oracle_spec import validate_oracle_spec
from trust.observation_mutate import complete_observations
from trust.trajectory_match import canonical_steps


def _spec(expected=None):
    return {"schema": "trace-eval.oracle-spec/v1", "nodes": {
        "step_0003": {"persistence": {"method": "reload",
                                        "expected": expected or {"enabled": True},
                                        "maxDelayMs": 5000}}}}


def _observations():
    return {"step_0003": {"target": {}, "action": {}, "reactions": {
        "persistence": {"method": "reload", "passed": True,
                        "observed": {"enabled": True}, "delayMs": 120,
                        "evidence": ["screenshot:after-reload", "selector:#enabled"],
                        "evidenceSource": "test-fixture"}}}}


def test_persistence_requirement_without_observation_is_unknown():
    trace = _good_trace()
    result = evaluate_test_case(trace, trace, oracle_spec=_spec())
    check = next(x for x in result["steps"][2]["checks"]
                 if x["criterion"] == "reaction.persistent_state")
    assert check["verdict"] == "unknown"


def test_reload_evidence_proves_persistent_state_and_final_outcome():
    trace = _good_trace()
    result = evaluate_test_case(trace, trace, observations=_observations(), oracle_spec=_spec())
    check = next(x for x in result["steps"][2]["checks"]
                 if x["criterion"] == "reaction.persistent_state")
    assert check["verdict"] == "pass"
    assert "screenshot:after-reload" in check["evidence"]


@pytest.mark.parametrize("mutation", ["wrong-value", "wrong-method", "too-late",
                                      "no-evidence", "unknown-source"])
def test_persistence_counterfactuals_fail(mutation):
    trace, observations = _good_trace(), _observations()
    value = observations["step_0003"]["reactions"]["persistence"]
    if mutation == "wrong-value": value["observed"] = {"enabled": False}
    elif mutation == "wrong-method": value["method"] = "api"
    elif mutation == "too-late": value["delayMs"] = 6000
    elif mutation == "no-evidence": value["evidence"] = []
    else: value["evidenceSource"] = "model-vibes"
    result = evaluate_test_case(trace, trace, observations=observations, oracle_spec=_spec())
    check = next(x for x in result["steps"][2]["checks"]
                 if x["criterion"] == "reaction.persistent_state")
    assert check["verdict"] == ("unknown" if mutation in {"no-evidence", "unknown-source"}
                                else "fail")


def test_oracle_spec_rejects_unknown_nodes_and_methods():
    with pytest.raises(ValueError, match="nodeId"):
        validate_oracle_spec({"schema": "trace-eval.oracle-spec/v1", "nodes": {
            "missing": {"persistence": {"method": "reload", "expected": {}}}}},
            node_ids={"step_0003"})
    bad = copy.deepcopy(_spec()); bad["nodes"]["step_0003"]["persistence"]["method"] = "sleep"
    with pytest.raises(ValueError, match="method"):
        validate_oracle_spec(bad, node_ids={"step_0003"})


def test_v2_flow_budgets_are_explicit_and_fail_when_exceeded():
    trace = _good_trace()
    spec = {"schema": "trace-eval.oracle-spec/v2", "nodes": {},
            "flow": {"maxExtraSteps": 0, "maxTotalRetries": 1, "maxDurationMs": 100}}
    observations = {step["nodeId"]: {
        "target": {}, "reactions": {},
        "action": {"retries": 1 if index == 0 else 0, "durationMs": 30}}
        for index, step in enumerate(canonical_steps(trace))}
    result = evaluate_test_case(trace, trace, observations=observations, oracle_spec=spec)
    checks = {row["criterion"]: row for row in result["flowChecks"]}
    assert checks["flow.retry_budget"]["verdict"] == "pass"
    assert checks["flow.duration_budget"]["verdict"] == "fail"
    assert checks["flow.path_efficiency"]["observed"]["efficiency"] == 1.0


def test_v2_rejects_negative_or_unknown_flow_budget():
    with pytest.raises(ValueError, match="flow"):
        validate_oracle_spec({"schema": "trace-eval.oracle-spec/v2", "nodes": {},
                              "flow": {"maxMagic": 1}}, node_ids=set())
    with pytest.raises(ValueError, match="非负"):
        validate_oracle_spec({"schema": "trace-eval.oracle-spec/v2", "nodes": {},
                              "flow": {"maxExtraSteps": -1}}, node_ids=set())


def _v3(flow):
    return {"schema": "trace-eval.oracle-spec/v3", "nodes": {}, "flow": flow}


def test_v3_optional_step_can_be_omitted_without_weakening_required_steps():
    reference = _good_trace()
    actual = copy.deepcopy(reference)
    actual["$meta"]["attach"]["entry"] = "step_0002"
    spec = _v3({"optionalNodeIds": ["step_0001"],
                "orderConstraints": [{"before": "step_0002", "after": "step_0003"}]})
    result = evaluate_test_case(actual, reference, oracle_spec=spec)
    checks = {row["criterion"]: row for row in result["flowChecks"]}
    assert checks["flow.required_steps"]["verdict"] == "pass"
    assert checks["flow.order_constraints"]["verdict"] == "pass"
    assert checks["flow.argument_conformance"]["verdict"] == "pass"
    assert checks["flow.path_efficiency"]["observed"]["efficiency"] == 1.0
    optional = result["steps"][0]["checks"][0]
    assert optional["criterion"] == "flow.optional_step"
    assert optional["verdict"] == "not_applicable"
    assert result["optionalExecution"] == {"declared": 1, "executed": 0, "omitted": 1}


def test_v3_partial_order_still_rejects_declared_dependency_violation():
    reference = _good_trace()
    actual = copy.deepcopy(reference)
    actual["step_0001"]["next"] = "step_0003"
    actual["step_0003"]["next"] = "step_0002"
    actual["step_0002"]["next"] = "step_0004"
    spec = _v3({"orderConstraints": [
        {"before": "step_0002", "after": "step_0003"}]})
    checks = {row["criterion"]: row for row in evaluate_test_case(
        actual, reference, oracle_spec=spec)["flowChecks"]}
    assert checks["flow.required_steps"]["verdict"] == "pass"
    assert checks["flow.order_constraints"]["verdict"] == "fail"


def test_v3_allowed_extra_action_is_not_misclassified_as_forbidden_or_inefficient():
    reference = _good_trace()
    actual = copy.deepcopy(reference)
    extra = copy.deepcopy(actual["step_0004"])
    extra["next"] = "step_0003"
    actual["step_extra"] = extra
    actual["step_0002"]["next"] = "step_extra"
    spec = _v3({"allowedExtraActions": ["DoNothing"]})
    checks = {row["criterion"]: row for row in evaluate_test_case(
        actual, reference, oracle_spec=spec)["flowChecks"]}
    assert checks["flow.forbidden_steps"]["verdict"] == "pass"
    assert checks["flow.path_efficiency"]["verdict"] == "pass"
    assert checks["flow.path_efficiency"]["observed"]["extraSteps"] == 0


def test_v3_rejects_unknown_optional_nodes_and_malformed_order_constraints():
    nodes = {"a", "b"}
    with pytest.raises(ValueError, match="optionalNodeIds"):
        validate_oracle_spec(_v3({"optionalNodeIds": ["missing"]}), node_ids=nodes)
    with pytest.raises(ValueError, match="orderConstraints"):
        validate_oracle_spec(_v3({"orderConstraints": [{"before": "a", "after": "a"}]}),
                             node_ids=nodes)


@pytest.mark.parametrize("path", [
    ["step_0001", "step_0003", "step_0002", "step_0004"],
    ["step_0002", "step_0003", "step_0004"],
    ["step_0001", "step_0003", "step_0004"],
])
def test_v3_legal_paths_have_consistent_reaction_and_flow_checks(path):
    reference = _good_trace()
    actual = copy.deepcopy(reference)
    actual["$meta"]["attach"]["entry"] = path[0]
    for index, node in enumerate(path):
        actual[node]["next"] = path[index + 1] if index + 1 < len(path) else None
    result = evaluate_test_case(actual, reference, observations=complete_observations(actual),
                                oracle_spec=_v3({"optionalNodeIds": (
                                    ["step_0002"] if "step_0002" not in path else []),
                                    "orderConstraints": [
                                    {"before": "step_0002", "after": "step_0004"}]}))
    assert result["summary"]["failed"] == 0
    assert result["summary"]["unknown"] == 0
    assert result["requiredStepSummary"]["evidenceCoverage"] == 1.0


def test_v3_omission_does_not_change_required_evidence_coverage():
    reference = _good_trace()
    actual = copy.deepcopy(reference)
    actual["$meta"]["attach"]["entry"] = "step_0002"
    full = evaluate_test_case(reference, reference, oracle_spec=_v3({}))
    omitted = evaluate_test_case(actual, reference, oracle_spec=_v3({}))
    assert full["requiredStepSummary"] == omitted["requiredStepSummary"]
    assert omitted["steps"][0]["summary"]["passed"] == 0


def test_v3_does_not_guess_identity_for_renamed_nodes():
    reference = _good_trace()
    actual = copy.deepcopy(reference)
    actual["renamed"] = actual.pop("step_0002")
    actual["step_0001"]["next"] = "renamed"
    result = evaluate_test_case(actual, reference, oracle_spec=_v3({}))
    checks = {item["criterion"]: item for item in result["flowChecks"]}
    assert checks["flow.required_steps"]["verdict"] == "fail"
    assert result["extraStepPolicy"]["disallowed"] == ["renamed"]


@pytest.mark.parametrize("flow", [
    {"optionalNodeIds": [[]]}, {"allowedExtraActions": [{}]},
    {"orderConstraints": [{"before": [], "after": "a"}]},
    {"orderConstraints": [{"before": "a", "after": "b"},
                          {"before": "b", "after": "a"}]},
])
def test_v3_malformed_constraints_fail_with_value_error(flow):
    with pytest.raises(ValueError, match="flow"):
        validate_oracle_spec(_v3(flow), node_ids={"a", "b"})


def test_downstream_assertion_must_belong_to_expected_successor():
    trace = _good_trace()
    observations = complete_observations(trace)
    observations["step_0003"]["reactions"]["downstreamAssertion"]["nodeId"] = "unrelated"
    result = evaluate_test_case(trace, trace, observations=observations)
    closure = next(item for item in result["steps"][2]["checks"]
                   if item["criterion"] == "reaction.downstream_assertion")
    assert closure["verdict"] == "fail"
