"""Round 3 验收：Experiment Runner 与 Replay Adapter。

逐条守设计文档 Round 3 的验收标准：

1. dry-run 不产生目标系统写操作；
2. Web 与 Maa smoke Case 各完成一次端到端 Run；
3. 执行异常、Ctrl-C 和业务失败后 cleanup 都被调用；
4. 进程在落盘中途终止不会产生被解析为 completed 的半份 Run；
5. 相同 Experiment 中 Run ID 唯一且可从 ID 定位全部 Artifact；
6. 认证失效被标记为 invalid，不计业务失败。
退出条件：一条命令产生可复查的单 Variant Experiment（CLI 测试）。
"""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import trace_eval.adapters as adapters_module
from trace_eval.adapters import (
    MaaReplayAdapter, WebReplayAdapter, ADAPTERS,
)
from trace_eval.artifacts import file_digest
from trace_eval.datasets import build_manifest, load_dataset
from trace_eval.runner import (
    RunConfig, RunnerError, run_experiment,
)
from trace_eval.artifacts import dataset_digest
from trace_eval.storage import (
    ExperimentStore, StorageError, RUN_JSON, RUN_TMP,
)

HERE = Path(__file__).resolve().parent / "fixtures" / "contracts"
FIXTURE = HERE / "dataset"
REPO_ROOT = HERE.parents[2]


def _snapshot(tmp_path: Path) -> Path:
    target = tmp_path / "snapshot"
    shutil.copytree(FIXTURE, target)
    return target


def _spy(real, behavior: str | None = None, case_id: str | None = None):
    calls = {"run": [], "cleanup": []}

    class Spy:
        name = real.name
        run_calls = calls["run"]
        cleanup_calls = calls["cleanup"]

        @staticmethod
        def preflight(case, variant):
            return real.preflight(case, variant)

        @staticmethod
        def prepare(case, workspace):
            return real.prepare(case, workspace)

        @staticmethod
        def run(prepared):
            if case_id and prepared.case_id != case_id:
                return real.run(prepared)
            calls["run"].append(prepared.case_id)
            if behavior == "raise":
                raise RuntimeError("boom（注入的执行异常）")
            if behavior == "interrupt":
                raise KeyboardInterrupt
            if behavior == "sleep":
                time.sleep(5)
            return real.run(prepared)

        @staticmethod
        def collect(prepared):
            return real.collect(prepared)

        @staticmethod
        def cleanup(prepared, outcome):
            calls["cleanup"].append(prepared.case_id)
            return real.cleanup(prepared, outcome)

    return Spy()


def _config(tmp_path: Path, snapshot: Path, **over) -> RunConfig:
    base = dict(experiment_id="exp-test", store_root=tmp_path / "store",
                snapshot_dir=snapshot)
    base.update(over)
    return RunConfig(**base)


# ── 1) dry-run 零写操作 ──────────────────────────────────────────


def _fs_state(paths: list[Path]) -> dict:
    state = {}
    for base in paths:
        for p in base.rglob("*"):
            if p.is_file():
                state[str(p)] = p.stat().st_size
    return state


def test_dry_run_performs_no_writes_and_reports_plan(tmp_path):
    snapshot = _snapshot(tmp_path)
    spy = _spy(ADAPTERS["maa"])
    adapters_module.ADAPTERS["maa"] = spy
    adapters_module.ADAPTERS["web"] = _spy(ADAPTERS["web"])
    try:
        before = _fs_state([snapshot, tmp_path / "store"])
        summary = run_experiment(_config(tmp_path, snapshot, dry_run=True))
        after = _fs_state([snapshot, tmp_path / "store"])
    finally:
        adapters_module.ADAPTERS["maa"] = MaaReplayAdapter()
        adapters_module.ADAPTERS["web"] = WebReplayAdapter()
    assert summary.dry_run_plan is not None
    assert {p.case_id for p in summary.dry_run_plan} == {"case-a", "case-b"}
    assert all(p.preflight_ok for p in summary.dry_run_plan)
    assert after == before            # 一个字节都没写
    assert spy.run_calls == []        # run 从未被调用


# ── 2) Web / Maa smoke 端到端 ────────────────────────────────────


def _find_run(summary, case_id: str) -> Path:
    for run_dir in summary.run_dirs:
        if run_dir.name.startswith(f"{case_id}__"):
            return run_dir
    raise AssertionError(f"找不到 {case_id} 的 Run：{summary.run_dirs}")


def test_maa_smoke_case_end_to_end(tmp_path):
    snapshot = _snapshot(tmp_path)
    summary = run_experiment(_config(tmp_path, snapshot))
    run_dir = _find_run(summary, "case-a")
    store = ExperimentStore(tmp_path / "store")
    run = store.load_run(run_dir)
    assert run.status == "completed"
    assert run.experiment_id == "exp-test"
    assert run.environment["adapter"] == "maa"
    assert run.environment["variantId"] == "v1"
    assert run.environment["trial"] == 1
    assert run.environment["datasetDigest"] == dataset_digest(snapshot)
    assert run.environment["gitSha"], "必须记录 Git SHA"
    for name in ("execution.json", "golden.json"):
        ref = run.artifacts[f"artifacts/{name}"]
        assert ref["digest"] == file_digest(run_dir / "artifacts" / name)
        assert (run_dir / "artifacts" / name).exists()


def test_web_smoke_case_end_to_end(tmp_path):
    snapshot = _snapshot(tmp_path)
    summary = run_experiment(_config(tmp_path, snapshot))
    run_dir = _find_run(summary, "case-b")
    run = ExperimentStore(tmp_path / "store").load_run(run_dir)
    assert run.status == "completed"
    assert run.environment["adapter"] == "web"
    for name in ("trace.json", "recording.json"):
        assert (run_dir / "artifacts" / name).exists()
    staged_trace = json.loads((run_dir / "workspace" / "trace.json").read_text())
    assert staged_trace["$meta"]["attach"]["nodeOrder"] == [
        "web-node-1", "web-node-2"]


# ── 5) Run ID 唯一且可定位全部 Artifact ───────────────────────────


def test_run_ids_unique_and_locate_all_artifacts(tmp_path):
    snapshot = _snapshot(tmp_path)
    summary = run_experiment(_config(tmp_path, snapshot))
    ids = [run_dir.name for run_dir in summary.run_dirs]
    assert len(ids) == len(set(ids))
    store = ExperimentStore(tmp_path / "store")
    for run_dir in summary.run_dirs:
        run = store.load_run(run_dir)
        for ref_name, ref in run.artifacts.items():
            target = run_dir / ref_name  # 从 Run 目录（由 ID 命名）即可定位
            assert target.exists()
            assert ref["digest"] == file_digest(target)


def test_rerun_produces_distinct_run_ids(tmp_path):
    snapshot = _snapshot(tmp_path)
    first = run_experiment(_config(tmp_path, snapshot, experiment_id="exp-1"))
    second = run_experiment(_config(tmp_path, snapshot, experiment_id="exp-2"))
    assert {d.name for d in first.run_dirs} != {d.name for d in second.run_dirs}


# ── 3) 异常 / Ctrl-C / 业务失败后 cleanup 都执行 ─────────────────


def test_cleanup_called_on_execution_exception(tmp_path):
    snapshot = _snapshot(tmp_path)
    spy = _spy(ADAPTERS["maa"], behavior="raise", case_id="case-a")
    adapters_module.ADAPTERS["maa"] = spy
    try:
        summary = run_experiment(_config(tmp_path, snapshot))
    finally:
        adapters_module.ADAPTERS["maa"] = MaaReplayAdapter()
    run_dir = _find_run(summary, "case-a")
    run = ExperimentStore(tmp_path / "store").load_run(run_dir)
    assert run.status == "infra_error"
    assert "RuntimeError" in (run.extra.get("note") or "")
    assert spy.cleanup_calls == ["case-a"]


def test_cleanup_called_on_keyboard_interrupt(tmp_path):
    snapshot = _snapshot(tmp_path)
    spy = _spy(ADAPTERS["maa"], behavior="interrupt", case_id="case-a")
    adapters_module.ADAPTERS["maa"] = spy
    try:
        summary = run_experiment(_config(tmp_path, snapshot))
    finally:
        adapters_module.ADAPTERS["maa"] = MaaReplayAdapter()
    run = ExperimentStore(tmp_path / "store").load_run(
        _find_run(summary, "case-a"))
    assert run.status == "cancelled"
    assert spy.cleanup_calls == ["case-a"]


def test_business_failure_still_cleans_up(tmp_path):
    """digest 不一致 = 完整性被拒（invalid），不是业务失败；cleanup 照跑。"""
    snapshot = _snapshot(tmp_path)
    execution = json.loads(
        (snapshot / "case-a" / "execution.json").read_text(encoding="utf-8"))
    execution["golden"] = dict(execution["golden"], digest="sha256:" + "0" * 64)
    (snapshot / "case-a" / "execution.json").write_text(
        json.dumps(execution), encoding="utf-8")
    # 重新登记 manifest：让「文件被改」这层门禁通过，
    # 专门测 adapter 层的逻辑一致性（Execution 声称的 Golden 摘要 vs 实际 Golden）
    build_manifest(snapshot)
    spy = _spy(ADAPTERS["maa"], case_id="case-a")
    adapters_module.ADAPTERS["maa"] = spy
    try:
        summary = run_experiment(_config(tmp_path, snapshot))
    finally:
        adapters_module.ADAPTERS["maa"] = MaaReplayAdapter()
    run = ExperimentStore(tmp_path / "store").load_run(
        _find_run(summary, "case-a"))
    assert run.status == "invalid"
    assert "不一致" in (run.extra.get("note") or "")
    assert spy.cleanup_calls == ["case-a"]


def test_timeout_marks_infra_error_and_cleans_up(tmp_path):
    snapshot = _snapshot(tmp_path)
    spy = _spy(ADAPTERS["maa"], behavior="sleep", case_id="case-a")
    adapters_module.ADAPTERS["maa"] = spy
    try:
        summary = run_experiment(_config(tmp_path, snapshot, timeout=0.3))
    finally:
        adapters_module.ADAPTERS["maa"] = MaaReplayAdapter()
    run = ExperimentStore(tmp_path / "store").load_run(
        _find_run(summary, "case-a"))
    assert run.status == "infra_error"
    assert "超时" in (run.extra.get("note") or "")
    assert spy.cleanup_calls == ["case-a"]


# ── 6) 认证失效 = invalid，不计业务失败 ──────────────────────────


def test_auth_block_marked_invalid_not_business_failure(tmp_path):
    snapshot = _snapshot(tmp_path)
    from trace_eval.adapters import PreflightResult
    real = MaaReplayAdapter()

    class AuthSpy:
        name = "maa"
        run_calls = []
        cleanup_calls = []

        @staticmethod
        def preflight(case, variant):
            if case.id == "case-a":
                return PreflightResult(ok=False, reason="认证令牌过期",
                                       blocked_kind="auth")
            return real.preflight(case, variant)

        @staticmethod
        def prepare(case, workspace):
            return real.prepare(case, workspace)

        @staticmethod
        def run(prepared):
            AuthSpy.run_calls.append(1)
            return real.run(prepared)

        @staticmethod
        def collect(prepared):
            return real.collect(prepared)

        @staticmethod
        def cleanup(prepared, outcome):
            AuthSpy.cleanup_calls.append(1)
            return real.cleanup(prepared, outcome)

    adapters_module.ADAPTERS["maa"] = AuthSpy()
    try:
        summary = run_experiment(_config(tmp_path, snapshot))
    finally:
        adapters_module.ADAPTERS["maa"] = MaaReplayAdapter()
    run = ExperimentStore(tmp_path / "store").load_run(
        _find_run(summary, "case-a"))
    assert run.status == "invalid"
    assert "认证" in (run.extra.get("note") or "")
    assert AuthSpy.run_calls == []      # 阻断后不执行
    other = ExperimentStore(tmp_path / "store").load_run(
        _find_run(summary, "case-b"))
    assert other.status == "completed"  # 其他 Case 不受影响


# ── Desktop 桩：环境类阻断 ───────────────────────────────────────


def test_desktop_stub_preflight_blocks_as_infra_error(tmp_path):
    snapshot = _snapshot(tmp_path)
    (snapshot / "case-c").mkdir()
    (snapshot / "case-c" / "recording.json").write_text(
        '{"steps": []}', encoding="utf-8")
    value = json.loads((snapshot / "dataset.json").read_text(encoding="utf-8"))
    value["cases"].append({"id": "case-c", "executor": "desktop",
                          "input": {"recording": "case-c/recording.json"}})
    (snapshot / "dataset.json").write_text(
        json.dumps(value, ensure_ascii=False), encoding="utf-8")
    build_manifest(snapshot)
    summary = run_experiment(_config(tmp_path, snapshot))
    run_dir = _find_run(summary, "case-c")
    run = ExperimentStore(tmp_path / "store").load_run(run_dir)
    assert run.status == "infra_error"
    assert "未接入" in (run.extra.get("note") or "")


# ── 4) 半份 Run 不可解析为 completed ────────────────────────────


def test_half_run_never_parses_as_completed(tmp_path):
    store = ExperimentStore(tmp_path / "store")
    handle = store.begin("exp-half", "sha256:x", {"dryRun": False})
    run_dir = store.run_dir(handle, store.new_run_id("case-a", "v1", 1))
    (run_dir / RUN_TMP).write_text('{"runId": "half", "status": "completed"}',
                                  encoding="utf-8")  # 模拟落盘中途进程终止
    assert not (run_dir / RUN_JSON).exists()
    with pytest.raises(StorageError):
        store.load_run(run_dir)


# ── 准入：只接受通过验证的 snapshot ─────────────────────────────


def test_experiment_requires_validated_snapshot(tmp_path):
    snapshot = _snapshot(tmp_path)
    (snapshot / "case-b" / "recording.json").write_text(
        '{"steps": [{"id": "tampered"}]}', encoding="utf-8")
    with pytest.raises(RunnerError, match="验证"):
        run_experiment(_config(tmp_path, snapshot))


# ── 退出条件：一条命令产生可复查的 Experiment ───────────────────


def test_cli_one_command_produces_reviewable_experiment(tmp_path):
    snapshot = _snapshot(tmp_path)
    store = tmp_path / "cli-store"
    result = subprocess.run(
        [sys.executable, "-m", "trace_eval.runner", "run", str(snapshot),
         "--store", str(store), "--dry-run"],
        capture_output=True, text=True, cwd=REPO_ROOT)
    assert result.returncode == 0
    assert "dry-run：未产生任何写操作" in result.stdout

    result = subprocess.run(
        [sys.executable, "-m", "trace_eval.runner", "run", str(snapshot),
         "--store", str(store), "--experiment-id", "exp-cli"],
        capture_output=True, text=True, cwd=REPO_ROOT)
    assert result.returncode == 0
    assert "completed" in result.stdout
    runs = list((store / "exp-cli" / "runs").iterdir())
    assert len(runs) == 2
    assert all((d / RUN_JSON).exists() for d in runs)
