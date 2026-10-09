from trust.challenge_compare import compare_challenge_reports


def _report(values, *, family="grounding", errors=0):
    runs = []
    for index, value in enumerate(values):
        runs.append({"id": f"c{index}", "family": family, "baselineId": "control",
                     "status": "provider-error" if index < errors else "success",
                     "claimRecordExact": value, "orderingDecreased": value})
    return {"promptHash": "hash-a", "runs": runs}


def test_six_paired_wins_are_significant_but_metric_is_not_probability():
    baseline = _report([False] * 6 + [True] * 14)
    candidate = _report([True] * 20)
    result = compare_challenge_reports(baseline, candidate, minimum_family_samples=5)
    metric = result["aggregate"]["orderingDetection"]
    assert metric["wins"] == 6 and metric["losses"] == 0
    assert metric["exactSignPValue"] == 0.03125
    assert metric["status"] == "significant-improvement"
    assert result["passed"] is True
    assert result["interpretation"].endswith("not a success probability")


def test_any_net_regression_fails_even_when_not_significant():
    baseline = _report([True] * 20)
    candidate = _report([False] + [True] * 19)
    result = compare_challenge_reports(baseline, candidate)
    assert result["aggregate"]["claimRecordExact"]["status"] == "regression"
    assert result["passed"] is False


def test_small_sample_refuses_to_claim_gate_pass():
    result = compare_challenge_reports(_report([False] * 4), _report([True] * 4),
                                       minimum_samples=5, minimum_family_samples=5)
    assert result["aggregate"]["claimRecordExact"]["status"] == "insufficient-data"
    assert result["passed"] is False


def test_missing_ids_and_more_provider_errors_fail_closed():
    baseline = _report([True] * 20)
    candidate = _report([True] * 19, errors=1)
    result = compare_challenge_reports(baseline, candidate)
    assert result["gates"]["sameChallengeIds"] is False
    assert result["gates"]["providerErrorsNotIncreased"] is False
    assert result["passed"] is False


def test_model_comparison_rejects_prompt_change_as_confounder():
    baseline, candidate = _report([True] * 20), _report([True] * 20)
    candidate["promptHash"] = "hash-b"
    result = compare_challenge_reports(baseline, candidate)
    assert result["gates"]["samePromptHash"] is False
    assert result["passed"] is False
