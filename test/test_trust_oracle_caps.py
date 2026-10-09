from test_trust_judge import EvidenceAwareProvider
from test_trust_mutate import _good_trace
from trust.evaluate import evaluate_trace
from trust.observation_mutate import complete_observations, mutate_observation
from trust.oracle_caps import oracle_score_caps


def test_only_definitive_fail_creates_caps_and_unknown_does_not():
    oracle = {
        "steps": [{"actualNodeId": "step_1", "referenceNodeId": "step_1", "checks": [
            {"criterion": "target.hit_point", "verdict": "fail", "evidence": ["outside"]},
            {"criterion": "reaction.assertion", "verdict": "unknown", "evidence": []},
        ]}],
        "flowChecks": [
            {"criterion": "flow.order_constraints", "verdict": "fail", "evidence": ["order"]},
            {"criterion": "flow.final_outcome", "verdict": "unknown", "evidence": []},
        ],
    }
    caps = oracle_score_caps(oracle)
    assert set(caps["steps"]["step_1"]) == {"action_correctness", "argument_quality"}
    assert caps["trajectory"].keys() == {"ordering"}
    assert "evidence_quality" not in caps["steps"]["step_1"]
    assert "evidence_closure" not in caps["trajectory"]


def test_missed_target_hard_caps_only_semantically_related_step_dimensions():
    trace = _good_trace()
    observations, _ = mutate_observation(
        trace, complete_observations(trace), "miss_target")
    result = evaluate_trace(trace, goal="保存策略", provider=EvidenceAwareProvider(),
                            observations=observations)
    capped = next(step for step in result["steps"]
                  if "action_correctness" in result["oracleCaps"]["steps"].get(
                      step["nodeId"], {}))
    assert capped["scores"]["action_correctness"] == 0
    assert capped["scores"]["argument_quality"] == 0
    assert capped["scores"]["evidence_quality"] > 0
    assert "target.hit_point" in capped["scoreEvidence"]["action_correctness"]["evidence"]


def test_failed_final_outcome_caps_trajectory_evidence_closure():
    trace = _good_trace()
    observations = complete_observations(trace)
    node_id = next(node for node, value in observations.items()
                   if value["reactions"].get("network"))
    observations[node_id]["reactions"]["network"][0]["status"] = 599
    observations[node_id]["reactions"]["network"][0]["ok"] = False
    observations[node_id]["reactions"]["network"][0]["validationEvidence"] = [
        "synthetic:response-validation-failed"]
    result = evaluate_trace(trace, goal="保存策略", provider=EvidenceAwareProvider(),
                            observations=observations)
    assert result["trajectory"]["evidence_closure"] == 0
    assert "flow.final_outcome" in result["trajectory"]["trajectoryEvidence"][
        "evidence_closure"]["evidence"]
    assert result["aggregate"]["hybridOrderingScore"] <= result["trajectory"]["overall"]
    assert result["aggregate"]["hybridOrderingScore"] <= result["oracle"]["summary"][
        "evidenceAdjustedOrderingScore"]
