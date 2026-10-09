import copy

import pytest

from test_trust_judge import EvidenceAwareProvider
from test_trust_mutate import _good_trace
from trust.challenge_run import run_challenges
from trust.challenge_set import build_challenge_set
from trust.review_queue import build_review_queue


def _inputs():
    challenges = build_challenge_set(_good_trace(), case="good")
    report = run_challenges(challenges, provider=EvidenceAwareProvider(), goal="保存策略")
    return challenges, report


def test_queue_is_blind_and_answer_key_is_separate():
    challenges, report = _inputs()
    result = build_review_queue(challenges, [report], limit=5)
    assert result["summary"]["selected"] == 5
    assert result["summary"]["truthExcludedFromBlindPayload"] is True
    for task in result["queue"]:
        assert "expected" not in task["blindPayload"]
        assert "oracle" not in task["blindPayload"]
        assert "modelTrials" not in task["blindPayload"]
    assert all(answer["expected"] for answer in result["answerKey"])
    assert {row["taskId"] for row in result["queue"]} == {
        row["taskId"] for row in result["answerKey"]}


def test_model_outputs_require_explicit_second_stage_mode():
    challenges, report = _inputs()
    result = build_review_queue(challenges, [report], limit=1,
                                include_model_outputs=True)
    assert result["summary"]["modelOutputsIncluded"] is True
    assert result["queue"][0]["blindPayload"]["modelTrials"]


def test_queue_prioritizes_errors_and_guarantees_family_coverage_when_budget_allows():
    challenges, report = _inputs()
    second = copy.deepcopy(report)
    target = next(row for row in second["runs"] if row["family"] == "grounding")
    target["status"] = "provider-error"; target["error"] = "bad evidence"
    result = build_review_queue(challenges, [report, second], limit=29, minimum_per_family=1)
    assert result["queue"][0]["selectionSignals"][0].startswith("provider-error")
    families = {row["failureFamily"] for row in challenges}
    assert set(result["summary"]["familyCounts"]) == families


def test_queue_rejects_mismatched_reports_and_bad_budget():
    challenges, report = _inputs()
    bad = copy.deepcopy(report); bad["runs"].pop()
    with pytest.raises(ValueError, match="IDs"):
        build_review_queue(challenges, [bad])
    with pytest.raises(ValueError, match="limit"):
        build_review_queue(challenges, [report], limit=0)
