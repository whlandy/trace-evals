from test_trust_judge import EvidenceAwareProvider
from test_trust_mutate import _good_trace
from trust.challenge_run import run_challenges
from trust.challenge_set import build_challenge_set


def test_challenge_report_audits_backlinks_and_counterfactual_attribution():
    challenges = build_challenge_set(_good_trace(), case="evidence")
    report = run_challenges(challenges, provider=EvidenceAwareProvider(), goal="保存策略")
    audit = report["evidenceAudit"]
    assert audit["aggregate"]["auditedRecords"] == len(challenges)
    assert audit["aggregate"]["backlinkPrecision"] == 1.0
    assert audit["aggregate"]["atomicCitationRate"] == 1.0
    assert audit["aggregate"]["counterfactualPairs"] == 30
    # This provider cites selectors but misses the missing-template finding. Hybrid
    # must not repair the model's citations before an independent evidence audit.
    assert audit["aggregate"]["evidenceChangeRate"] == round(29 / 30, 4)
    assert audit["aggregate"]["defectCitationRate"] == round(29 / 30, 4)
    assert audit["contractGate"]["failures"] == [
        "counterfactual-evidence-did-not-change", "counterfactual-defect-not-cited"]
    assert audit["contractGate"]["passed"] is False
    missed = [row for row in audit["pairs"] if not row["defectCited"]]
    assert len(missed) == 1 and ":drop_template:" in missed[0]["id"]
    assert report["modelOutputSource"] == "validated-judge-before-hybrid"
    assert audit["aggregate"]["interpretation"].endswith("not accuracy or probability")


def test_audit_passes_when_provider_itself_cites_changed_rule_evidence():
    class CitesFindings(EvidenceAwareProvider):
        identity = "test:cites-findings"

        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            for step, context in zip(result["steps"], kwargs["payload"]["steps"]):
                step["scoreEvidence"]["replay_safety"]["evidence"].extend(
                    finding["evidence"] for finding in context["ruleFindings"])
            return result

    report = run_challenges(build_challenge_set(_good_trace(), case="complete"),
                            provider=CitesFindings(), goal="保存策略")
    assert report["evidenceAudit"]["contractGate"]["passed"] is True


def test_truth_citation_requires_actual_reference_not_merely_nonempty_truth():
    from trust.evidence_audit import _cites_truth

    assert not _cites_truth(set(), {"target.hit_point"})
    assert not _cites_truth({"unrelated"}, {"target.hit_point"})
    assert _cites_truth({"target.hit_point"}, {"target.hit_point"})
    assert _cites_truth({"oracle:step_1:target.hit_point:fail:digest"}, {"target.hit_point"})


def test_valid_but_hallucinated_extra_citation_lowers_precision():
    challenges = build_challenge_set(_good_trace(), case="extra")[:1]
    report = run_challenges(challenges, provider=EvidenceAwareProvider(), goal="保存策略")
    # 审计器也用于读取旧版/外部报告；直接注入一条历史坏引用，不能依赖 v7 validator 放行。
    report["runs"][0]["modelOutputs"][0]["scoreEvidence"][
        "action_correctness"]["evidence"].append("invented:looks-right")
    from trust.evidence_audit import audit_challenge_evidence
    audit = audit_challenge_evidence(challenges, report["runs"])
    assert audit["aggregate"]["backlinkPrecision"] < 1
    assert audit["contractGate"] == {
        "passed": False, "failures": ["ungrounded-citation"],
        "policy": "full coverage and exact deterministic evidence invariants"}
    assert audit["records"][0]["ungrounded"][0]["token"] == "invented:looks-right"


def test_provider_errors_remain_in_evidence_coverage_denominator():
    class Broken(EvidenceAwareProvider):
        identity = "test:broken-evidence-audit"

        def complete_json(self, **kwargs):
            raise RuntimeError("offline")

    challenges = build_challenge_set(_good_trace(), case="broken")[:2]
    audit = run_challenges(challenges, provider=Broken(), goal="x")["evidenceAudit"]
    assert audit["aggregate"]["records"] == 2
    assert audit["aggregate"]["auditedRecords"] == 0
    assert audit["records"][0]["status"] == "unavailable"
    assert "not-all-records-audited" in audit["contractGate"]["failures"]
