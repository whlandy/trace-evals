from test_trust_mutate import _good_trace
from trust.challenge_set import (OBSERVATION_FAMILIES, PERSISTENCE_MUTATIONS,
                                 build_challenge_set, validate_challenge_set)
from trust.mutate import MUTATIONS


def test_challenge_set_covers_every_registered_failure_shape():
    rows = build_challenge_set(_good_trace(), case="good")
    report = validate_challenge_set(rows)
    assert report["valid"] is True, report["failures"]
    assert report["records"] == 2 + len(OBSERVATION_FAMILIES) + len(MUTATIONS) + len(PERSISTENCE_MUTATIONS)
    assert report["controls"] == 2
    assert report["counterfactuals"] == 30


def test_every_counterfactual_has_evidence_bearing_changed_check():
    rows = build_challenge_set(_good_trace(), case="good")
    for row in rows:
        if row["baselineId"] is None:
            continue
        assert row["expected"]["changedChecks"], row["id"]
        assert all(item["reason"] for item in row["expected"]["changedChecks"])
        assert row["interpretation"].endswith("not a calibrated success probability")


def test_challenge_ids_are_stable_and_inputs_are_self_contained():
    first = build_challenge_set(_good_trace(), case="good")
    second = build_challenge_set(_good_trace(), case="good")
    assert [row["id"] for row in first] == [row["id"] for row in second]
    sample = next(row for row in first if row["mutation"] == "wrong_semantic_target")
    assert sample["input"]["trace"]
    assert sample["input"]["reference"]
    assert sample["input"]["observations"]
    assert sample["expected"]["stepClaims"]
