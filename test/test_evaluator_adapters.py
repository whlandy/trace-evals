"""Round 1 验收：统一 Contract 与 Result 协议。

逐条守设计文档 Round 1 的验收标准：

1. 现有 audit 和 Maa evaluator 都能转换为 EvaluatorResult[]；
2. 每个 fail 结果具有 key、failureCode、evidence 和 evaluator version；
3. 未知主版本被拒绝，未知附加字段可保留；
4. JSON round-trip 不丢字段；
5. 新协议结果与 Round 0 核心结论完全一致（fail 节点、verdict、分数逐项对照
   golden-results/ —— Round 0 冻结的字节级基线）。
"""

import json
from pathlib import Path

import pytest

from trace_eval.contracts import (
    SCHEMA_CASE, SCHEMA_RESULT, SCHEMA_RUN,
    ContractError, EvalCase, EvaluatorResult, EvalRun,
    parse_artifact_ref, parse_eval_case, parse_eval_run,
    parse_evaluator_result, roundtrip,
)
from trace_eval.evaluators.execution import MaaExecutionEvaluator
from trace_eval.evaluators.golden_trust import GoldenTrustEvaluator

HERE = Path(__file__).resolve().parent / "fixtures" / "contracts"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _golden_result(name: str):
    return _load(HERE / "golden-results" / name)


# ── 1) audit() 可转换为 EvaluatorResult[] ──────────────────────────


def test_golden_trust_converts_weak_case_findings_one_to_one():
    """每条 trust finding 恰好对应一条 fail 结果 —— 不丢、不造。"""
    results = GoldenTrustEvaluator().evaluate(HERE / "cases" / "case-weak")
    golden = _golden_result("audit-case-weak.json")
    findings = list(golden["findings"]) + list(golden["crosscheck"])
    fails = [r for r in results if r.verdict == "fail" and r.key != "golden.trust.overall"]
    assert len(fails) == len(findings)

    def sort_key(pair):
        finding, result = pair
        return (finding["failure"], finding.get("node") or "", result.key)

    for finding, result in sorted(zip(findings, fails), key=sort_key):
        assert result.failure_code == (
            f"TRUST_{finding['axis'].upper()}_{finding['failure'].upper()}")
        assert result.node_id == finding.get("node")
        assert finding["evidence"] in result.evidence


def test_golden_trust_clean_case_is_all_pass():
    results = GoldenTrustEvaluator().evaluate(HERE / "cases" / "case-success")
    assert results
    assert all(r.verdict != "fail" for r in results)
    overall = next(r for r in results if r.key == "golden.trust.overall")
    golden = _golden_result("audit-case-success.json")
    assert overall.verdict == "pass"
    assert overall.score == golden["score"]


def test_golden_trust_missing_recording_is_inconclusive_not_fail():
    """观测缺失报 inconclusive —— 缺失不是失败，猜才是。"""
    results = GoldenTrustEvaluator().evaluate(HERE / "cases" / "case-weak")
    recording = next(r for r in results if r.key == "golden.trust.recording")
    assert recording.verdict == "inconclusive"
    assert recording.failure_code is None


# ── 1)+5) Maa evaluator 转换，且与 Round 0 基线一致 ───────────────


@pytest.mark.parametrize("scenario, verdict, golden", [
    ("success", "pass", "maa-success.json"),
    ("fail", "fail", "maa-fail.json"),
    ("optional-skip", "pass", "maa-optional-skip.json"),
])
def test_execution_adapter_matches_round0_golden(scenario, verdict, golden):
    results = MaaExecutionEvaluator().evaluate(
        _load(HERE / "maa" / "golden.json"),
        _load(HERE / "maa" / f"execution-{scenario}.json"))
    expect = _golden_result(golden)
    task = next(r for r in results if r.key == "execution.maa.task")
    assert task.verdict == verdict
    assert task.score == expect["score"]
    if verdict == "pass":
        assert task.failure_code is None and task.node_id is None
    else:
        assert task.failure_code == "TASK_NOT_SUCCESSFUL"
        assert expect["failedNodeIds"] == [task.node_id]
        assert any(task.node_id in item for item in task.evidence)
    metrics = next(r for r in results if r.key == "execution.maa.metrics")
    if scenario == "fail":
        assert metrics.verdict == "fail"
        assert metrics.observed["stepCompletionRate"] == expect["stepCompletionRate"]
    else:
        assert metrics.verdict == "pass"


def test_execution_adapter_rejects_digest_mismatch_as_hard_fail():
    execution = _load(HERE / "maa" / "execution-success.json")
    execution["golden"] = dict(execution["golden"], digest="sha256:" + "0" * 64)
    results = MaaExecutionEvaluator().evaluate(
        _load(HERE / "maa" / "golden.json"), execution)
    assert len(results) == 1
    result = results[0]
    assert result.verdict == "fail"
    assert result.failure_code == "EVALUATION_REJECTED"
    assert result.evidence


def test_incomplete_golden_rejected_before_any_scoring():
    golden = _load(HERE / "maa" / "golden.json")
    golden["$meta"]["attach"]["status"] = "incomplete"
    results = MaaExecutionEvaluator().evaluate(golden,
                                              _load(HERE / "maa" / "execution-success.json"))
    assert len(results) == 1
    assert results[0].failure_code == "EVALUATION_REJECTED"


# ── 2) 每个 fail 结果都有 key/failureCode/evidence/evaluator 版本 ──


def _ok_result(**over) -> dict:
    base = {"schema": SCHEMA_RESULT, "key": "test.key", "scope": "run",
            "verdict": "pass",
            "evaluator": {"name": "n", "version": "1.0.0"}}
    base.update(over)
    return base


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(verdict="fail"),                                   # 无 failureCode
    lambda r: r.update(verdict="fail", failureCode="X"),                  # 无 evidence
    lambda r: r.update(verdict="fail", failureCode="X", evidence=[]),     # 空 evidence
    lambda r: r.update(verdict="fail", failureCode="X",
                       evidence=["e"], evaluator={"name": "n"}),          # 无版本
])
def test_fail_result_requires_code_evidence_and_version(mutate):
    result = _ok_result()
    mutate(result)
    with pytest.raises(ContractError):
        parse_evaluator_result(result)


def test_valid_fail_result_parses():
    parsed = parse_evaluator_result(_ok_result(
        verdict="fail", failureCode="X", evidence=["e"]))
    assert parsed.to_dict()["failureCode"] == "X"


# ── 3) 未知主版本拒绝 / 附加字段保留 ───────────────────────────────


def test_unknown_major_version_rejected_with_clear_message():
    with pytest.raises(ContractError, match="未知主版本"):
        parse_evaluator_result(_ok_result(schema="trace-evals.result/v2"))
    with pytest.raises(ContractError):
        parse_evaluator_result(_ok_result(schema="some.other.family/v1"))


def test_unknown_extra_fields_preserved_not_dropped():
    parsed = parse_evaluator_result(
        _ok_result(**{"futureField": 42, "nested": {"a": 1}}))
    assert parsed.extra == {"futureField": 42, "nested": {"a": 1}}
    assert parsed.to_dict()["futureField"] == 42


def test_invalid_enum_and_type_rejected():
    with pytest.raises(ContractError):
        parse_evaluator_result(_ok_result(scope="whole"))
    with pytest.raises(ContractError):
        parse_evaluator_result(_ok_result(verdict="maybe"))
    with pytest.raises(ContractError):
        parse_evaluator_result(_ok_result(score=True))
    with pytest.raises(ContractError):
        parse_evaluator_result(_ok_result(calibration="vibes"))


# ── 4) JSON round-trip 不丢字段 ───────────────────────────────────


def test_result_roundtrip_lossless():
    result = parse_evaluator_result(_ok_result(
        score=0.5, calibration="deterministic", nodeId="step_0002",
        expected={"a": 1}, observed={"a": 0}, evidence=["e1", "e2"],
        comment="c", futureField=42))
    again = roundtrip(result)
    assert again.to_dict() == result.to_dict()


def test_run_roundtrip_lossless_and_status_strict():
    run = {"schema": SCHEMA_RUN, "runId": "exp-1/case-a/v1/01",
           "experimentId": "exp-1", "caseId": "case-a", "variantId": "v1",
           "trial": 1, "status": "completed",
           "artifacts": {"golden": {"path": "a.json", "digest": "sha256:0"}},
           "futureField": 42}
    parsed = parse_eval_run(run)
    assert roundtrip(parsed).to_dict() == parsed.to_dict()
    with pytest.raises(ContractError):
        parse_eval_run(dict(run, status="weird"))
    with pytest.raises(ContractError):
        parse_eval_run(dict(run, trial="1"))
    with pytest.raises(ContractError):
        parse_eval_run(dict(run, trial=-1))


def test_case_roundtrip_lossless_and_required_fields():
    case = {"schema": SCHEMA_CASE, "id": "case-a", "executor": "maa",
            "input": {"golden": "a.json"}, "tags": ["t"]}
    parsed = parse_eval_case(case)
    assert roundtrip(parsed).to_dict() == parsed.to_dict()
    assert parsed.tags == ["t"]
    with pytest.raises(ContractError):
        parse_eval_case({"schema": SCHEMA_CASE, "id": "", "executor": "maa",
                         "input": {}})


def test_artifact_ref_strict_path():
    ref = parse_artifact_ref({"path": "a.json", "digest": "sha256:0"})
    assert ref.to_dict() == {"path": "a.json", "digest": "sha256:0"}
    with pytest.raises(ContractError):
        parse_artifact_ref({"path": ""})
