from test_trust_judge import EvidenceAwareProvider
from test_trust_mutate import _good_trace
from trust.challenge_run import behavior_contract_gate, run_challenges
from trust.challenge_set import build_challenge_set


def test_runner_reports_each_failure_family_and_does_not_call_scores_probability():
    challenges = build_challenge_set(_good_trace(), case="good")
    report = run_challenges(challenges, provider=EvidenceAwareProvider(), goal="保存策略")
    assert report["aggregate"]["records"] == 32
    assert report["aggregate"]["providerErrors"] == 0
    assert report["aggregate"]["claimExactAccuracy"] == 1.0
    assert "grounding" in report["byFailureFamily"]
    assert "not independent model accuracy or success probability" in report["aggregate"]["interpretation"]
    # provider 本身只看静态 findings；CODE oracle 的合取聚合必须补上运行观测盲区。
    assert report["byFailureFamily"]["grounding"]["orderingSensitivity"] == 1.0
    assert report["behaviorGate"] == {
        "passed": True, "failures": [], "failedFamilies": [],
        "coverage": {"records": 32, "counterfactuals": 30, "orderingChecked": 30},
        "policy": "exact synthetic claims and strict counterfactual monotonicity",
        "interpretation": "deterministic challenge contract; not deployment accuracy"}


def test_provider_contract_violation_is_isolated_per_challenge():
    class EmptyEvidence(EvidenceAwareProvider):
        identity = "test:empty-evidence"
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            result["steps"][0]["claims"]["target_execution"]["evidence"] = []
            return result

    challenges = build_challenge_set(_good_trace(), case="good")[:2]
    report = run_challenges(challenges, provider=EmptyEvidence(), goal="保存策略")
    assert report["aggregate"]["providerErrors"] == 2
    assert all(row["status"] == "provider-error" for row in report["runs"])
    assert {row["errorType"] for row in report["runs"]} == {"ValueError"}
    assert report["behaviorGate"]["passed"] is False
    assert "provider-errors" in report["behaviorGate"]["failures"]
    assert "not-all-counterfactuals-checked" in report["behaviorGate"]["failures"]


def test_behavior_gate_rejects_aggregate_and_family_blind_spots():
    gate = behavior_contract_gate(
        records=3, counterfactuals=2, provider_errors=0,
        claim_exact_accuracy=0.9, ordering_sensitivity=0.5, ordering_checked=2,
        families={"grounding": {"orderingChecked": 1, "orderingSensitivity": 0.0},
                  "reaction": {"orderingChecked": 1, "orderingSensitivity": 1.0}})
    assert gate["passed"] is False
    assert gate["failedFamilies"] == ["grounding"]
    assert gate["failures"] == [
        "claim-not-exact", "counterfactual-ordering-not-monotonic",
        "family-ordering-not-monotonic"]
