"""Round 4 验收：C1–C4 分层报告与 trace_correct 合取判定。

逐条守设计文档 Round 4 的验收标准（判定部分）：

- 「执行全成功但后端状态错误」→ 最终必须 fail；
- 「业务正确但 Execution digest 不匹配」→ 最终必须 fail；
- 「执行与业务均通过但 Golden 不可信」→ 分层展示，不折叠成一个绿色总分；
- 某层缺证据 → inconclusive，不得自动按通过处理（含：无 Oracle 的写 Case）。
"""

import json
from pathlib import Path

from trace_eval.contracts import EvalRun
from trace_eval.correctness import (
    LayerVerdict, judge, render_report, structure_results,
)
from trace_eval.evaluators.execution import MaaExecutionEvaluator
from trace_eval.evaluators.golden_trust import GoldenTrustEvaluator
from trace_eval.evaluators.oracles import evaluate_oracles
from trace_eval.storage import ExperimentStore

HERE = Path(__file__).resolve().parent / "fixtures" / "contracts"


def _load(name: Path):
    return json.loads(name.read_text(encoding="utf-8"))


def _make_run(tmp_path: Path, case_id: str,
              artifacts: dict) -> Path:
    """直接落一个已定稿的 Run（跳过 runner，单元级构造四层证据的输入）。"""
    store = ExperimentStore(tmp_path / "store")
    handle = store.begin("exp-judge", "sha256:test", {})
    run_dir = store.run_dir(handle, store.new_run_id(case_id, "v1", 1))
    art = run_dir / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    for name, content in artifacts.items():
        (art / name).write_text(
            content if isinstance(content, str)
            else json.dumps(content, ensure_ascii=False), encoding="utf-8")
    store.commit_run(run_dir, EvalRun(
        run_id=run_dir.name, experiment_id="exp-judge", case_id=case_id,
        variant_id="v1", trial=1, status="completed",
        environment={"adapter": "maa", "datasetDigest": "sha256:test"}),
        results=[])
    return run_dir


def _golden_trust_results(case: str) -> list:
    return GoldenTrustEvaluator().evaluate(HERE / "cases" / case)


def _maa_results() -> tuple[dict, dict]:
    golden = _load(HERE / "maa" / "golden.json")
    execution = _load(HERE / "maa" / "execution-success.json")
    return golden, execution


def _oracle_result(run_dir: Path, expect: str) -> list:
    """C3：api-json Oracle（业务结果层）。"""
    from types import SimpleNamespace
    return evaluate_oracles(
        SimpleNamespace(oracles=[{
            "schema": "trace-evals.oracle/v1", "type": "api-json",
            "name": "policy", "evidence": "artifacts/response.json",
            "jsonPath": "state", "expect": {"equals": expect}}]),
        run_dir)


def test_execution_success_but_backend_state_wrong_is_fail(tmp_path):
    """执行全成功 + 结构完整 + Golden 可信，但后端状态错 → 最终 fail。"""
    golden, execution = _maa_results()
    run_dir = _make_run(tmp_path, "case-backend-wrong", {
        "golden.json": golden, "execution.json": execution,
        "response.json": {"state": "disabled"},
    })
    results = (structure_results(run_dir)
               + MaaExecutionEvaluator().evaluate(golden, execution)
               + _oracle_result(run_dir, "enabled")
               + _golden_trust_results("case-success"))
    judgment = judge(results)
    assert judgment.trace_correct == "fail"
    assert judgment.layers["C3"].verdict == "fail"
    assert judgment.layers["C2"].verdict == "pass"  # 执行层本身没问题
    report = render_report(judgment)
    assert "business_oracles_passed" in report
    assert "fail" in report


def test_business_right_but_execution_digest_mismatch_is_fail(tmp_path):
    """业务对、但 Execution 与 Golden 摘要不符 → 最终 fail（完整性硬失败）。"""
    golden, execution = _maa_results()
    execution = json.loads(json.dumps(execution))
    execution["golden"] = dict(execution["golden"],
                               digest="sha256:" + "0" * 64)
    run_dir = _make_run(tmp_path, "case-digest-mismatch", {
        "golden.json": golden, "execution.json": execution,
        "response.json": {"state": "enabled"},
    })
    results = (structure_results(run_dir)
               + MaaExecutionEvaluator().evaluate(golden, execution)
               + _oracle_result(run_dir, "enabled")
               + _golden_trust_results("case-success"))
    judgment = judge(results)
    assert judgment.trace_correct == "fail"
    assert judgment.layers["C2"].verdict == "fail"
    assert judgment.layers["C3"].verdict == "pass"  # 业务确实是对的


def test_clean_execution_and_business_but_untrusted_golden_is_fail(tmp_path):
    """执行与业务均通过，Golden 不可信 → fail，且分层展示不折叠。"""
    golden, execution = _maa_results()
    run_dir = _make_run(tmp_path, "case-weak-golden", {
        "golden.json": golden, "execution.json": execution,
        "response.json": {"state": "enabled"},
    })
    results = (structure_results(run_dir)
               + MaaExecutionEvaluator().evaluate(golden, execution)
               + _oracle_result(run_dir, "enabled")
               + _golden_trust_results("case-weak"))  # Golden 层有 findings
    judgment = judge(results)
    assert judgment.trace_correct == "fail"
    assert judgment.layers["C4"].verdict == "fail"
    assert judgment.layers["C1"].verdict == "pass"
    assert judgment.layers["C2"].verdict == "pass"
    assert judgment.layers["C3"].verdict == "pass"
    report = render_report(judgment)
    # 四层各自独立成行，verdict 各不相同可见 —— 不是单一绿色总分
    for label in ("structure_valid", "execution_conformant",
                  "business_oracles_passed", "golden_accepted"):
        assert label in report


def test_write_case_without_oracle_is_inconclusive(tmp_path):
    """写 Case 没有 Oracle 证据 → C3 inconclusive → 整体不得为 pass。"""
    golden, execution = _maa_results()
    run_dir = _make_run(tmp_path, "case-no-oracle", {
        "golden.json": golden, "execution.json": execution,
    })
    results = (structure_results(run_dir)
               + MaaExecutionEvaluator().evaluate(golden, execution)
               + _golden_trust_results("case-success"))
    judgment = judge(results)
    assert judgment.trace_correct == "inconclusive"
    assert judgment.layers["C3"].verdict == "inconclusive"
    assert "C3" in (judgment.note or "")


def test_all_four_layers_pass_but_stability_missing_is_inconclusive(tmp_path):
    """C1–C4 全过但稳定性门未建立（Round 4 阶段）→ 不允许全绿。"""
    golden, execution = _maa_results()
    run_dir = _make_run(tmp_path, "case-clean", {
        "golden.json": golden, "execution.json": execution,
        "response.json": {"state": "enabled"},
    })
    results = (structure_results(run_dir)
               + MaaExecutionEvaluator().evaluate(golden, execution)
               + _oracle_result(run_dir, "enabled")
               + _golden_trust_results("case-success"))
    judgment = judge(results)
    assert judgment.trace_correct == "inconclusive"
    assert "稳定性" in (judgment.note or "")


def test_all_five_gates_pass_is_pass(tmp_path):
    golden, execution = _maa_results()
    run_dir = _make_run(tmp_path, "case-stable", {
        "golden.json": golden, "execution.json": execution,
        "response.json": {"state": "enabled"},
    })
    results = (structure_results(run_dir)
               + MaaExecutionEvaluator().evaluate(golden, execution)
               + _oracle_result(run_dir, "enabled")
               + _golden_trust_results("case-success"))
    judgment = judge(results, stability=LayerVerdict(
        "C5", "stability_gate_passed", "pass", ["3/3 trial 一致"]))
    assert judgment.trace_correct == "pass"


def test_fail_beats_inconclusive(tmp_path):
    """任一层 fail 优先于其他层的 inconclusive。"""
    golden, execution = _maa_results()
    execution = json.loads(json.dumps(execution))
    execution["golden"] = dict(execution["golden"],
                               digest="sha256:" + "0" * 64)
    run_dir = _make_run(tmp_path, "case-fail-first", {
        "golden.json": golden, "execution.json": execution,
    })
    results = (structure_results(run_dir)
               + MaaExecutionEvaluator().evaluate(golden, execution)
               + _golden_trust_results("case-success"))
    # C3 无证据（inconclusive），C2 fail → 整体必须 fail
    judgment = judge(results)
    assert judgment.trace_correct == "fail"


def test_structure_layer_detects_web_node_step_mismatch(tmp_path):
    run_dir = _make_run(tmp_path, "case-web-mismatch", {
        "trace.json": {"nodes": [{"id": "n1"}, {"id": "n2"}]},
        "recording.json": {"steps": [{"id": "n1"}]},
    })
    results = structure_results(run_dir)
    assert len(results) == 1
    assert results[0].verdict == "fail"
    assert results[0].failure_code == "STRUCTURE_NODE_STEP_MISMATCH"
    assert results[0].expected and results[0].observed  # 证据齐全
