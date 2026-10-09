import pytest

from test_trust_mutate import _good_trace
from trust.observation_mutate import (OBSERVATION_MUTATIONS, complete_observations,
                                      mutate_observation)
from trust.oracle import evaluate_test_case


@pytest.mark.parametrize("name", OBSERVATION_MUTATIONS)
def test_every_runtime_counterfactual_lowers_evidence_adjusted_score(name):
    trace = _good_trace()
    baseline = complete_observations(trace)
    before = evaluate_test_case(trace, trace, observations=baseline)["summary"]
    mutated, description = mutate_observation(trace, baseline, name)
    after = evaluate_test_case(trace, trace, observations=mutated)["summary"]
    assert after["evidenceAdjustedOrderingScore"] < before["evidenceAdjustedOrderingScore"], (
        f"{name}（{description}）：{before} → {after}")


def test_missing_reaction_reduces_coverage_instead_of_becoming_a_pass():
    trace = _good_trace()
    baseline = complete_observations(trace)
    mutated, _ = mutate_observation(trace, baseline, "missing_network_reaction")
    result = evaluate_test_case(trace, trace, observations=mutated)["summary"]
    assert result["unknown"] > 0
    assert result["evidenceCoverage"] < 1
    assert result["interpretation"] == "ordering-only-not-a-probability"
