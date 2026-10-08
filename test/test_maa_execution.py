import pytest

from trust.maa_execution import EvaluationError, evaluate, trace_digest


def _golden():
    node = {
        "recognition": {"type": "TemplateMatch", "param": {"template": "save.png"}},
        "action": {"type": "Click", "param": {}},
        "next": [],
        "attach": {},
    }
    meta = {
        "recognition": {"type": "DirectHit", "param": {}},
        "action": {"type": "DoNothing", "param": {}},
        "next": [],
        "attach": {
            "schema": "edr.success-trace/v2",
            "status": "ready",
            "nodeOrder": ["step_0001"],
        },
    }
    return {"step_0001": node, "$meta": meta}


def _execution(golden, *, status="success", step_status="success"):
    return {
        "schema": "edr.maa-execution-trace/v1",
        "golden": {
            "digest": trace_digest(golden),
            "nodeOrder": ["step_0001"],
        },
        "status": status,
        "steps": [{
            "nodeId": "step_0001",
            "status": step_status,
            "actualAction": "Click",
            "matchScore": 0.92,
            "retries": 0,
        }],
    }


def test_success_requires_exact_successful_path():
    golden = _golden()
    report = evaluate(golden, _execution(golden))

    assert report["taskSuccess"] is True
    assert report["score"] == 99.6
    assert report["failedNodeIds"] == []


def test_failed_runtime_is_not_scored_as_success():
    golden = _golden()
    report = evaluate(golden, _execution(golden, status="failed", step_status="failed"))

    assert report["taskSuccess"] is False
    assert report["failedNodeIds"] == ["step_0001"]
    assert report["stepCompletionRate"] == 0


def test_digest_mismatch_is_rejected():
    golden = _golden()
    execution = _execution(golden)
    execution["golden"]["digest"] = "sha256:wrong"

    with pytest.raises(EvaluationError, match="different golden"):
        evaluate(golden, execution)


def test_incomplete_golden_is_rejected_before_scoring():
    golden = _golden()
    golden["$meta"]["attach"]["status"] = "incomplete"

    with pytest.raises(EvaluationError, match="not ready"):
        evaluate(golden, _execution(golden))


def test_missing_golden_node_is_rejected_before_scoring():
    golden = _golden()
    del golden["step_0001"]

    with pytest.raises(EvaluationError, match="missing nodes"):
        evaluate(golden, _execution(golden))


def test_skipped_step_is_satisfied_only_when_golden_marks_it_optional():
    golden = _golden()
    golden["step_0001"]["attach"] = {"provenance": {"optional": True}}
    execution = _execution(golden, step_status="skipped")
    execution["steps"][0]["actualAction"] = None

    report = evaluate(golden, execution)

    assert report["taskSuccess"] is True
    assert report["stepCompletionRate"] == 1.0
    assert report["failedNodeIds"] == []

    golden["step_0001"]["attach"]["provenance"]["optional"] = False
    execution["golden"]["digest"] = trace_digest(golden)
    assert evaluate(golden, execution)["taskSuccess"] is False


def test_duplicate_execution_node_cannot_pass():
    golden = _golden()
    execution = _execution(golden)
    execution["steps"].append(dict(execution["steps"][0]))

    report = evaluate(golden, execution)

    assert report["taskSuccess"] is False
    assert report["duplicateNodeIds"] == ["step_0001"]
