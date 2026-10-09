import pytest

from test_trust_mutate import _good_trace
from trust.eval_types import validate_step_evaluation
from trust.mutate import mutate
from trust.step_score import score_steps


def _by_id(result):
    return {step["nodeId"]: step for step in result["steps"]}


def test_every_node_gets_a_valid_multidimensional_score():
    result = score_steps(_good_trace())
    assert result["steps"]
    for step in result["steps"]:
        validate_step_evaluation(step)
        assert step["confidence"] == "deterministic"


def test_node_finding_lowers_only_the_affected_step():
    trace, _ = mutate(_good_trace(), "blind_toggle")
    result = score_steps(trace)
    affected = [s for s in result["steps"] if any(
        f["rule"] == "blind_toggle" for f in s["findings"])]
    assert len(affected) == 1
    assert affected[0]["scores"]["replay_safety"] == 0.0
    assert affected[0]["overall"] < 1.0


def test_trace_level_findings_are_not_falsely_attributed_to_a_node():
    trace, _ = mutate(_good_trace(), "drop_assertions")
    result = score_steps(trace)
    assert any(f["rule"] == "no_assertions" for f in result["traceFindings"])
    assert not any(f["rule"] == "no_assertions"
                   for step in result["steps"] for f in step["findings"])


def test_aggregate_reports_tail_risk_not_only_the_mean():
    trace, _ = mutate(_good_trace(), "blind_toggle")
    aggregate = score_steps(trace)["aggregate"]
    assert aggregate["minimum"] <= aggregate["bottomKMean"] <= aggregate["mean"]
    assert aggregate["interpretation"] == "uncalibrated-ordering-only"


def test_schema_rejects_out_of_range_scores():
    step = _by_id(score_steps(_good_trace()))["step_0001"]
    step["scores"]["context_fit"] = 1.1
    with pytest.raises(ValueError, match="context_fit"):
        validate_step_evaluation(step)
