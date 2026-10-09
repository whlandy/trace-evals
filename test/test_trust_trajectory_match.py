import copy

import pytest

from test_trust_mutate import _good_trace
from trust.trajectory_match import match_trajectories


def _rewire(trace, order):
    result = copy.deepcopy(trace)
    result["$meta"]["attach"]["entry"] = order[0] if order else None
    for index, node_id in enumerate(order):
        result[node_id]["next"] = [order[index + 1]] if index + 1 < len(order) else []
    return result


def test_strict_requires_exact_order_but_reports_partial_credit():
    reference = _good_trace()
    actual = _rewire(reference, ["step_0001", "step_0003", "step_0002", "step_0004"])
    result = match_trajectories(actual, reference, "strict")
    assert result["passed"] is False
    assert 0 < result["score"] < 1
    assert result["orderMismatch"] is True


def test_unordered_accepts_same_steps_in_another_order():
    reference = _good_trace()
    actual = _rewire(reference, ["step_0002", "step_0001", "step_0003", "step_0004"])
    assert match_trajectories(actual, reference, "unordered")["passed"] is True


def test_subset_allows_missing_reference_steps_but_not_extras():
    reference = _good_trace()
    actual = _rewire(reference, ["step_0001", "step_0002"])
    assert match_trajectories(actual, reference, "subset")["passed"] is True
    assert match_trajectories(reference, actual, "subset")["passed"] is False


def test_superset_requires_all_reference_steps_and_allows_extras():
    full = _good_trace()
    short = _rewire(full, ["step_0001", "step_0002"])
    assert match_trajectories(full, short, "superset")["passed"] is True
    result = match_trajectories(short, full, "superset")
    assert result["passed"] is False
    assert len(result["missing"]) == 2


def test_same_action_with_changed_arguments_is_reported_separately():
    reference = _good_trace()
    actual = copy.deepcopy(reference)
    actual["step_0002"]["action"]["param"]["state"] = False
    result = match_trajectories(actual, reference)
    assert result["passed"] is False
    assert result["argumentMismatches"][0]["index"] == 1


def test_unknown_mode_fails_closed():
    with pytest.raises(ValueError, match="mode"):
        match_trajectories(_good_trace(), _good_trace(), "loose")
