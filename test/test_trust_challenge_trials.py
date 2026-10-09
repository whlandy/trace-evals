import copy

import pytest

from trust.challenge_trials import analyze_trials


def _report(outcomes, *, provider="model-a", ids=None):
    ids = ids or ["c1", "c2"]
    runs = []
    for challenge_id, passed in zip(ids, outcomes):
        runs.append({"id": challenge_id, "family": "grounding", "mutation": "miss",
                     "baselineId": "control", "status": "success",
                     "claimRecordExact": passed, "orderingDecreased": passed,
                     "rubricVersion": "v3"})
    return {"provider": provider, "promptId": "canonical", "promptHash": "hash-a",
            "runs": runs}


def test_pass_at_k_and_pass_power_k_expose_flaky_success():
    reports = [_report([True, False]), _report([False, False]), _report([True, True])]
    result = analyze_trials(reports)
    assert result["k"] == 3
    assert result["aggregate"]["claimPassAtK"] == 1.0
    assert result["aggregate"]["claimPassPowerK"] == 0.0
    assert result["aggregate"]["claimFlipRate"] == 1.0
    assert result["aggregate"]["orderingPassAtK"] == 1.0
    assert result["stabilityGate"]["passed"] is False
    assert "claimPassPowerK-not-one" in result["stabilityGate"]["failures"]
    assert result["interpretation"].endswith("not a calibrated deployment probability")


def test_provider_error_counts_as_failed_trial_not_missing_data():
    first, second = _report([True, True]), _report([True, True])
    second["runs"][0]["status"] = "provider-error"
    result = analyze_trials([first, second])
    row = next(item for item in result["records"] if item["id"] == "c1")
    assert row["claimOutcomes"] == [True, False]
    assert result["aggregate"]["providerErrorTrialRate"] == 0.25
    assert "providerErrorTrialRate-not-zero" in result["stabilityGate"]["failures"]


def test_perfect_repeated_contract_passes_stability_gate():
    result = analyze_trials([_report([True, True]), _report([True, True])])
    assert result["stabilityGate"] == {
        "passed": True, "failures": [],
        "policy": "all synthetic contracts pass in every independent trial with zero flips",
        "interpretation": "stability contract; not deployment success probability"}


def test_trials_must_be_independent_reports_for_same_contract():
    with pytest.raises(ValueError, match="至少"):
        analyze_trials([_report([True, True])])
    with pytest.raises(ValueError, match="provider"):
        analyze_trials([_report([True, True]), _report([True, True], provider="model-b")])
    with pytest.raises(ValueError, match="IDs"):
        analyze_trials([_report([True, True]), _report([True], ids=["c1"])])
    changed = _report([True, True]); changed["runs"][0]["rubricVersion"] = "v4"
    with pytest.raises(ValueError, match="rubricVersion"):
        analyze_trials([_report([True, True]), changed])
    changed = _report([True, True]); changed["promptHash"] = "hash-b"
    with pytest.raises(ValueError, match="promptId/promptHash"):
        analyze_trials([_report([True, True]), changed])
