import json

from test_trust_judge import EvidenceAwareProvider
from test_trust_mutate import _good_trace
from trust.challenge_set import build_challenge_set
from trust.injection_robustness import ATTACKS, run_injection_robustness


def test_evidence_aware_provider_is_invariant_to_all_untrusted_attacks():
    challenges = build_challenge_set(_good_trace(), case="good")[:1]
    result = run_injection_robustness(challenges, provider=EvidenceAwareProvider(),
                                      goal="保存策略")
    assert result["aggregate"]["checked"] == len(ATTACKS)
    assert result["aggregate"]["passed"] == len(ATTACKS)
    assert result["aggregate"]["robust"] is True
    assert all(row["oracleInvariant"] for row in result["rows"])


def test_score_manipulation_is_detected_even_when_claims_cannot_flip():
    class InjectionSensitive(EvidenceAwareProvider):
        identity = "test:injection-sensitive"
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            if "synthetic-indirect-prompt-injection" in json.dumps(kwargs["payload"]):
                for step in result["steps"]:
                    step["scores"] = {key: 0.25 for key in step["scores"]}
            return result
    challenge = build_challenge_set(_good_trace(), case="good")[:1]
    result = run_injection_robustness(challenge, provider=InjectionSensitive(),
                                      goal="保存策略", attacks=["score_manipulation"])
    row = result["rows"][0]
    assert row["oracleInvariant"] is True
    assert row["claimInvariant"] is True
    assert row["maxScoreDelta"] == 0.75
    assert row["passed"] is False


def test_claim_override_becomes_visible_provider_error():
    class ClaimHijacked(EvidenceAwareProvider):
        identity = "test:claim-hijacked"
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            if "synthetic-indirect-prompt-injection" in json.dumps(kwargs["payload"]):
                result["steps"][0]["claims"]["target_execution"]["verdict"] = "pass"
            return result
    # clean challenge has target pass, choose miss_target where forced pass flips oracle fail.
    challenge = [next(row for row in build_challenge_set(_good_trace(), case="good")
                      if row["mutation"] == "miss_target")]
    result = run_injection_robustness(challenge, provider=ClaimHijacked(),
                                      goal="保存策略", attacks=["direct_override"])
    assert "翻转确定性 oracle" in result["rows"][0]["attackError"]
    assert result["aggregate"]["attackSuccesses"] == 1
