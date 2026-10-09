import copy

import pytest

from test_trust_mutate import _good_trace
from trust.hybrid import hybrid_evaluation
from trust.evaluate import evaluate_trace
from trust.judge import build_payload, judge_trace
from trust.judge_validate import (calibration_rows, failure_node_hits,
                                  validate_judge, write_calibration_jsonl)


class EvidenceAwareProvider:
    identity = "test:evidence-aware:v1"

    def __init__(self):
        self.calls = 0

    def complete_json(self, *, system, payload, schema):
        self.calls += 1
        steps = []
        for context in payload["steps"]:
            findings = context["ruleFindings"]
            penalty = min(0.8, 0.2 * len(findings))
            scores = {key: 1.0 - penalty for key in payload["dimensions"]}
            score_evidence = {
                "action_correctness": {
                    "evidence": [next(x["evidenceId"] for x in context["oracleChecks"]
                                      if x["criterion"] == "action.completed")],
                    "reason": "依据动作完成状态"},
                "argument_quality": {"evidence": [context["fieldEvidence"]["current.selector"]],
                                     "reason": "依据选择器"},
                "context_fit": {"evidence": [context["fieldEvidence"]["before"]],
                                "reason": "依据上下文"},
                "evidence_quality": {
                    "evidence": [context["oracleChecks"][0]["evidenceId"]],
                    "reason": "依据 oracle"},
                "replay_safety": {"evidence": [context["fieldEvidence"]["current.selector"]],
                                  "reason": "依据规则"},
            }
            def claim(prefix):
                relevant = [item for item in context["oracleChecks"]
                            if item["criterion"].startswith(prefix)]
                verdict = ("fail" if any(x["verdict"] == "fail" for x in relevant) else
                           "unknown" if any(x["verdict"] == "unknown" for x in relevant) else
                           "pass" if any(x["verdict"] == "pass" for x in relevant) else
                           "not_applicable")
                return {"verdict": verdict,
                        "evidence": [x["evidenceId"] for x in relevant],
                        "reason": "依据确定性 oracle"}
            steps.append({"nodeId": context["current"]["nodeId"], "scores": scores,
                          "scoreEvidence": score_evidence,
                          "claims": {"target_execution": claim("target."),
                                     "post_action_reaction": claim("reaction."),
                                     "testcase_conformance": claim("flow.")},
                          "reason": "依据 nodeId 和 ruleFindings 评分"})
        trace_penalty = min(0.8, 0.2 * len(payload["traceFindings"]))
        return {"steps": steps, "trajectory": {
            "completeness": 1 - trace_penalty, "necessity": 1,
            "ordering": 1, "evidence_closure": 1 - trace_penalty,
            "trajectoryEvidence": {
                "completeness": {"evidence": [payload["trajectoryEvidenceIds"]["flowOracle"]],
                                 "reason": "依据流程完整性"},
                "necessity": {"evidence": [payload["trajectoryEvidenceIds"]["steps"]],
                              "reason": "依据全部步骤"},
                "ordering": {"evidence": [payload["trajectoryEvidenceIds"]["referenceSteps"]],
                             "reason": "依据参考顺序"},
                "evidence_closure": {
                    "evidence": ([payload["flowOracle"]["checks"][0]["evidenceId"]]
                                 if payload["flowOracle"]["checks"] else ["flowOracle"]),
                    "reason": "依据 outcome oracle"},
            },
            "reason": "依据完整轨迹评分"}}


def test_judge_normalizes_strict_model_output_and_marks_it_uncalibrated():
    result = judge_trace(_good_trace(), goal="保存策略", provider=EvidenceAwareProvider())
    assert result["steps"][0]["confidence"] == "uncalibrated-judge-score"
    assert result["trajectory"]["overall"] <= 1
    assert result["provenance"]["rubricVersion"]
    assert result["steps"][0]["claims"]["target_execution"]["verdict"] == "unknown"


def test_cache_uses_input_and_provider_identity(tmp_path):
    provider = EvidenceAwareProvider()
    judge_trace(_good_trace(), goal="保存策略", provider=provider, cache_dir=tmp_path)
    second = judge_trace(_good_trace(), goal="保存策略", provider=provider, cache_dir=tmp_path)
    assert provider.calls == 1
    assert second["provenance"]["cacheHit"] is True


def test_prompt_text_is_part_of_cache_key_and_provenance(tmp_path):
    provider = EvidenceAwareProvider()
    first = judge_trace(_good_trace(), goal="x", provider=provider,
                        system_prompt="prompt-a", cache_dir=tmp_path)
    second = judge_trace(_good_trace(), goal="x", provider=provider,
                         system_prompt="prompt-b", cache_dir=tmp_path)
    assert provider.calls == 2
    assert first["provenance"]["inputHash"] != second["provenance"]["inputHash"]
    assert first["provenance"]["promptHash"] != second["provenance"]["promptHash"]


def test_wrong_node_set_fails_closed():
    class Bad(EvidenceAwareProvider):
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            result["steps"][0]["nodeId"] = "invented"
            return result
    with pytest.raises(ValueError, match="节点"):
        judge_trace(_good_trace(), goal="x", provider=Bad())


def test_provider_cannot_smuggle_extra_fields_past_local_validation():
    class Bad(EvidenceAwareProvider):
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            result["steps"][0]["unsupported"] = True
            return result
    with pytest.raises(ValueError, match="字段"):
        judge_trace(_good_trace(), goal="x", provider=Bad())


def test_provider_cannot_turn_missing_observation_into_a_pass():
    class Bad(EvidenceAwareProvider):
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            result["steps"][0]["claims"]["target_execution"]["verdict"] = "pass"
            return result
    with pytest.raises(ValueError, match="翻转确定性 oracle"):
        judge_trace(_good_trace(), goal="x", provider=Bad())


def test_claim_must_cite_nonempty_relevant_evidence():
    class Empty(EvidenceAwareProvider):
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            result["steps"][0]["claims"]["target_execution"]["evidence"] = []
            return result
    with pytest.raises(ValueError, match="至少一条"):
        judge_trace(_good_trace(), goal="x", provider=Empty())

    class Invented(EvidenceAwareProvider):
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            result["steps"][0]["claims"]["target_execution"]["evidence"] = ["looks-right"]
            return result
    with pytest.raises(ValueError, match="未引用"):
        judge_trace(_good_trace(), goal="x", provider=Invented())

    class CriterionOnly(EvidenceAwareProvider):
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            result["steps"][0]["claims"]["target_execution"]["evidence"] = [
                kwargs["payload"]["steps"][0]["oracleChecks"][0]["criterion"]]
            return result
    with pytest.raises(ValueError, match="带状态"):
        judge_trace(_good_trace(), goal="x", provider=CriterionOnly())


def test_each_score_dimension_must_have_grounded_evidence():
    class Empty(EvidenceAwareProvider):
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            result["steps"][0]["scoreEvidence"]["action_correctness"]["evidence"] = []
            return result
    with pytest.raises(ValueError, match="scoreEvidence.action_correctness"):
        judge_trace(_good_trace(), goal="x", provider=Empty())

    class Invented(EvidenceAwareProvider):
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            result["steps"][0]["scoreEvidence"]["replay_safety"]["evidence"] = ["vibes"]
            return result
    with pytest.raises(ValueError, match="未回链"):
        judge_trace(_good_trace(), goal="x", provider=Invented())

    class Mixed(EvidenceAwareProvider):
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            result["steps"][0]["scoreEvidence"]["action_correctness"]["evidence"].append(
                "invented:covered-by-valid-token")
            return result
    with pytest.raises(ValueError, match="未回链"):
        judge_trace(_good_trace(), goal="x", provider=Mixed())


def test_generic_step_field_name_cannot_support_a_score_by_itself():
    class GenericOnly(EvidenceAwareProvider):
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            result["steps"][0]["scoreEvidence"]["argument_quality"]["evidence"] = [
                "current.selector"]
            return result

    with pytest.raises(ValueError, match="必须引用原子证据"):
        judge_trace(_good_trace(), goal="x", provider=GenericOnly())


def test_each_trajectory_dimension_must_have_grounded_evidence():
    class Empty(EvidenceAwareProvider):
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            result["trajectory"]["trajectoryEvidence"]["ordering"]["evidence"] = []
            return result
    with pytest.raises(ValueError, match="trajectoryEvidence.ordering"):
        judge_trace(_good_trace(), goal="x", provider=Empty())

    class Invented(EvidenceAwareProvider):
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            result["trajectory"]["trajectoryEvidence"]["necessity"]["evidence"] = ["vibes"]
            return result
    with pytest.raises(ValueError, match="未回链"):
        judge_trace(_good_trace(), goal="x", provider=Invented())

    class Mixed(EvidenceAwareProvider):
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            result["trajectory"]["trajectoryEvidence"]["ordering"]["evidence"].append(
                "invented:ordering")
            return result
    with pytest.raises(ValueError, match="未回链"):
        judge_trace(_good_trace(), goal="x", provider=Mixed())


def test_generic_trajectory_field_name_cannot_support_a_score_by_itself():
    class GenericOnly(EvidenceAwareProvider):
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            result["trajectory"]["trajectoryEvidence"]["ordering"]["evidence"] = [
                "referenceSteps"]
            return result

    with pytest.raises(ValueError, match="必须引用原子证据"):
        judge_trace(_good_trace(), goal="x", provider=GenericOnly())


def test_hybrid_never_raises_a_dimension_rejected_by_rules():
    trace = _good_trace()
    trace["step_0002"]["action"] = {"type": "Click", "param": {}}
    # selector 带 toggle，触发 blind_toggle；provider 仍给满分。
    judged = judge_trace(trace, goal="x", provider=EvidenceAwareProvider())
    hybrid = hybrid_evaluation(trace, judged)
    step = next(s for s in hybrid["steps"] if s["nodeId"] == "step_0002")
    assert step["scores"]["replay_safety"] == 0
    assert step["overall"] <= 0.49
    assert hybrid["aggregate"]["interpretation"] == "ordering-only-not-a-probability"


def test_meta_eval_covers_all_mutations_and_enforces_zero_conflicts():
    report = validate_judge(_good_trace(), goal="保存策略",
                            provider=EvidenceAwareProvider(), repetitions=2)
    sensitivity = report["mutationSensitivity"]
    assert sensitivity["checked"] == len(sensitivity["rows"]) > 0
    assert sensitivity["passed"] == sensitivity["checked"]
    assert report["structure"]["valid"] is True
    assert report["repeatConsistency"]["labelAgreement"] == 1
    assert report["ruleConflicts"]["afterConservativeMerge"] == 0
    counterfactual = report["oracleCounterfactualSensitivity"]
    assert counterfactual["passed"] == counterfactual["checked"] == 15


def test_failure_node_metric_and_calibration_export_keep_raw_labels(tmp_path):
    trace = _good_trace()
    trace["step_0002"]["action"] = {"type": "Click", "param": {}}
    judged = judge_trace(trace, goal="保存策略", provider=EvidenceAwareProvider())
    cases = {"case-a": (trace, judged)}
    labels = {"case-a": {"label": "stable-red",
                          "runs": [{"failedNode": "step_0002"}]}}
    metric = failure_node_hits(cases, labels)
    assert metric["hits"] == metric["checked"] == 1
    rows = calibration_rows(cases, labels)
    assert rows[0]["targetLabel"] == "stable-red"
    assert rows[0]["confidence"] == "uncalibrated-judge-score"
    output = tmp_path / "calibration.jsonl"
    write_calibration_jsonl(output, rows)
    assert '"targetLabel": "stable-red"' in output.read_text()


def test_unified_evaluation_includes_all_match_modes_when_reference_is_given():
    trace = _good_trace()
    result = evaluate_trace(trace, goal="保存策略", provider=EvidenceAwareProvider(),
                            reference=copy.deepcopy(trace))
    assert set(result["match"]) == {"strict", "unordered", "subset", "superset"}
    assert all(item["passed"] for item in result["match"].values())
    assert result["oracle"]["summary"]["unknown"] > 0


def test_openai_provider_uses_strict_json_schema_without_network():
    class Response:
        output_text = '{"ok": true}'

    class Responses:
        def __init__(self): self.kwargs = None
        def create(self, **kwargs):
            self.kwargs = kwargs
            return Response()

    class Client:
        def __init__(self): self.responses = Responses()

    from trust.providers.openai_provider import OpenAIProvider
    client = Client()
    provider = OpenAIProvider("test-model", client=client)
    result = provider.complete_json(system="s", payload={"x": 1}, schema={"type": "object"})
    assert result == {"ok": True}
    assert client.responses.kwargs["text"]["format"]["strict"] is True


def test_oracle_spec_is_exposed_to_judge_as_atomic_persistence_check():
    trace = _good_trace()
    spec = {"schema": "trace-eval.oracle-spec/v1", "nodes": {
        "step_0003": {"persistence": {"method": "reload",
                                        "expected": {"enabled": True}}}}}
    payload = build_payload(trace, goal="保存策略", oracle_spec=spec)
    step = next(item for item in payload["steps"] if item["current"]["nodeId"] == "step_0003")
    persistence = next(item for item in step["oracleChecks"]
                       if item["criterion"] == "reaction.persistent_state")
    assert persistence["verdict"] == "unknown"
    assert persistence["expected"]["method"] == "reload"


def test_v3_optional_omission_keeps_judge_oracle_checks_aligned_by_node_id():
    trace = _good_trace()
    actual = copy.deepcopy(trace)
    actual["$meta"]["attach"]["entry"] = "step_0002"
    spec = {"schema": "trace-eval.oracle-spec/v3", "nodes": {},
            "flow": {"optionalNodeIds": ["step_0001"]}}
    payload = build_payload(actual, goal="保存策略", reference=trace, oracle_spec=spec)
    assert payload["steps"][0]["current"]["nodeId"] == "step_0002"
    criteria = {item["criterion"] for item in payload["steps"][0]["oracleChecks"]}
    assert "flow.step_semantics" in criteria
    assert "flow.optional_step" not in criteria


def test_v3_allowed_extra_step_has_its_own_permission_and_runtime_checks():
    trace = _good_trace()
    actual = copy.deepcopy(trace)
    extra = copy.deepcopy(actual["step_0004"])
    extra["next"] = "step_0003"
    actual["step_extra"] = extra
    actual["step_0002"]["next"] = "step_extra"
    spec = {"schema": "trace-eval.oracle-spec/v3", "nodes": {},
            "flow": {"allowedExtraActions": ["DoNothing"]}}
    payload = build_payload(actual, goal="保存策略", reference=trace, oracle_spec=spec)
    context = next(item for item in payload["steps"]
                   if item["current"]["nodeId"] == "step_extra")
    checks = {item["criterion"]: item["verdict"] for item in context["oracleChecks"]}
    assert checks["flow.allowed_extra_step"] == "pass"
    assert checks["action.completed"] == "unknown"
    assert "flow.step_semantics" not in checks


@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.parametrize("completed", [True, False, None])
def test_extra_steps_are_evaluated_end_to_end_and_capped(allowed, completed):
    from trust.observation_mutate import complete_observations

    trace = _good_trace()
    actual = copy.deepcopy(trace)
    actual["step_extra"] = copy.deepcopy(actual["step_0004"])
    actual["step_extra"]["next"] = "step_0003"
    actual["step_0002"]["next"] = "step_extra"
    spec = {"schema": "trace-eval.oracle-spec/v3", "nodes": {},
            "flow": {"allowedExtraActions": ["DoNothing"] if allowed else []}}
    observations = complete_observations(actual)
    if completed is None:
        del observations["step_extra"]
    else:
        observations["step_extra"]["action"]["completed"] = completed
    result = evaluate_trace(actual, goal="保存策略", provider=EvidenceAwareProvider(),
                            reference=trace, observations=observations, oracle_spec=spec)
    step = next(item for item in result["steps"] if item["nodeId"] == "step_extra")
    raw = next(item for item in result["judge"]["steps"] if item["nodeId"] == "step_extra")
    oracle = next(item for item in result["oracle"]["steps"]
                  if item["actualNodeId"] == "step_extra")
    checks = {item["criterion"]: item for item in oracle["checks"]}
    criterion = "flow.allowed_extra_step" if allowed else "flow.extra_step"
    assert checks[criterion]["verdict"] == ("pass" if allowed else "fail")
    assert checks["action.completed"]["verdict"] == (
        "unknown" if completed is None else "pass" if completed else "fail")
    if allowed and completed is True:
        assert result["oracle"]["summary"]["failed"] == 0
        assert result["oracle"]["summary"]["unknown"] == 0
    if not allowed or completed is False:
        assert step["scores"]["action_correctness"] == 0
        assert raw["scores"]["action_correctness"] > 0
        assert "确定性 oracle fail" not in raw["scoreEvidence"]["action_correctness"]["reason"]
    payload = build_payload(actual, goal="保存策略", reference=trace,
                            observations=observations, oracle_spec=spec)
    context = next(item for item in payload["steps"] if item["current"]["nodeId"] == "step_extra")
    assert [{key: value for key, value in item.items() if key != "evidenceId"}
            for item in context["oracleChecks"]] == oracle["checks"]


@pytest.mark.parametrize("insertion", ["tail", "middle"])
def test_strict_extra_also_has_runtime_and_flow_checks(insertion):
    from trust.observation_mutate import complete_observations

    trace = _good_trace()
    actual = copy.deepcopy(trace)
    if insertion == "tail":
        actual["step_extra"] = copy.deepcopy(actual["step_0004"])
        actual["step_0004"]["next"] = "step_extra"
    else:
        actual["step_extra"] = copy.deepcopy(actual["step_0002"])
        actual["step_extra"]["action"]["param"]["state"] = False
        actual["step_0002"]["next"] = "step_extra"
    result = evaluate_trace(actual, goal="保存策略", reference=trace,
                            observations=complete_observations(actual), provider=EvidenceAwareProvider())
    assert {step["actualNodeId"] for step in result["oracle"]["steps"]} == {
        step["nodeId"] for step in result["steps"]}
    assert result["oracleCaps"]["steps"]
