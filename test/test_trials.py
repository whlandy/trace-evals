"""Round 5 验收：重复 Trial、状态策略与稳定性。

逐条守设计文档 Round 5 的验收标准：

1. {pass, pass} → stable-green；
2. {fail, fail} → stable-red；
3. {pass, fail} → flip；
4. 全 pass 但关键轨迹指标不同 → drifting；
5. 任一 Trial 认证失效 → invalid（不得错误产生 stable-red）；
6. 未声明隔离的写 Case 不并发；
7. carry-forward 能暴露人工构造的状态依赖缺陷。
退出条件：Case 稳定性来自统一 Experiment（stability.jsonl），不是独立脚本。
"""

import json
import shutil
from pathlib import Path

import pytest

from trace_eval.adapters import ADAPTERS, PreflightResult
from trace_eval.aggregation import stability_label, stability_layer
from trace_eval.correctness import judge, structure_results
from trace_eval.datasets import build_manifest
from trace_eval.evaluators.golden_trust import GoldenTrustEvaluator
from trace_eval.runner import (
    RunConfig, WriteLockRegistry, WriteLockError, run_experiment,
)
from trace_eval.state_policies import policy_name_of
from trace_eval.storage import ExperimentStore

HERE = Path(__file__).resolve().parent / "fixtures" / "contracts"
FIXTURE = HERE / "dataset"


def _snapshot(tmp_path: Path) -> Path:
    target = tmp_path / "snapshot"
    shutil.copytree(FIXTURE, target)
    return target


def _config(tmp_path: Path, snapshot: Path, **over) -> RunConfig:
    base = dict(experiment_id="exp-trials", store_root=tmp_path / "store",
                snapshot_dir=snapshot)
    base.update(over)
    return RunConfig(**base)


def _edit_dataset(snapshot: Path) -> dict:
    return json.loads((snapshot / "dataset.json").read_text(encoding="utf-8"))


def _save_dataset(snapshot: Path, value: dict) -> None:
    (snapshot / "dataset.json").write_text(
        json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _passing_oracle_response(snapshot: Path) -> None:
    """给 case-a 加一个会通过的 api-json Oracle + 证据 Artifact。"""
    (snapshot / "case-a" / "response.json").write_text(
        json.dumps({"state": "enabled"}), encoding="utf-8")
    value = _edit_dataset(snapshot)
    case_a = next(c for c in value["cases"] if c["id"] == "case-a")
    case_a["input"]["evidence"] = "case-a/response.json"
    case_a["oracles"] = [{
        "schema": "trace-evals.oracle/v1", "type": "api-json",
        "name": "backend-state", "evidence": "artifacts/response.json",
        "jsonPath": "state", "expect": {"equals": "enabled"}}]
    _save_dataset(snapshot, value)
    build_manifest(snapshot)


# ── 1) {pass, pass} → stable-green，且五门全过 = 首次全绿 ─────────


def test_stable_green_and_first_full_green(tmp_path):
    snapshot = _snapshot(tmp_path)
    _passing_oracle_response(snapshot)
    summary = run_experiment(_config(tmp_path, snapshot, trials=2))

    stab = summary.stability["case-a"]
    assert stab["label"] == "stable-green"
    assert stab["verdict"] == "pass"
    assert all(t["taskSuccess"] for t in stab["trials"])

    # 五门合取：C1–C4 + C5 全过 → trace_correct = pass
    run_dir = next(d for d in summary.run_dirs if d.name.startswith("case-a"))
    store = ExperimentStore(tmp_path / "store")
    results = (structure_results(run_dir)
               + store.load_results(run_dir)
               + GoldenTrustEvaluator().evaluate(HERE / "cases" / "case-success"))
    judgment = judge(results,
                    stability=stability_layer("case-a", "stable-green",
                                             stab["trials"]))
    assert judgment.trace_correct == "pass"
    assert {k: v.verdict for k, v in judgment.layers.items()} == {
        "C1": "pass", "C2": "pass", "C3": "pass", "C4": "pass", "C5": "pass"}


# ── 2) {fail, fail} → stable-red ────────────────────────────────


def test_stable_red_label(tmp_path):
    snapshot = _snapshot(tmp_path)
    _passing_oracle_response(snapshot)
    value = _edit_dataset(snapshot)
    next(c for c in value["cases"] if c["id"] == "case-a")["oracles"][0]["expect"] = \
        {"equals": "never-this"}
    _save_dataset(snapshot, value)
    build_manifest(snapshot)
    summary = run_experiment(_config(tmp_path, snapshot, trials=2))
    stab = summary.stability["case-a"]
    assert stab["label"] == "stable-red"
    assert stab["verdict"] == "fail"
    assert all(not t["taskSuccess"] for t in stab["trials"])


# ── 3) + 7) {pass, fail} → flip；carry-forward 暴露状态依赖缺陷 ──


def _clean_state_oracle(snapshot: Path, policy: str) -> None:
    """case-b 加一个「系统状态必须干净」的 Oracle —— 人工构造的状态依赖缺陷：
    它的通过与否取决于**上一遍是否写过了系统**，而不是流程本身。"""
    value = _edit_dataset(snapshot)
    case_b = next(c for c in value["cases"] if c["id"] == "case-b")
    case_b["environment"]["statePolicy"] = {"policy": policy}
    case_b["oracles"] = [{
        "schema": "trace-evals.oracle/v1", "type": "file-content",
        "name": "clean-system-state",
        "evidence": "artifacts/state-store.json",
        "expect": {"contains": "{}"}}]
    _save_dataset(snapshot, value)
    build_manifest(snapshot)


def test_flip_via_carry_forward_exposes_state_defect(tmp_path):
    snapshot = _snapshot(tmp_path)
    _clean_state_oracle(snapshot, "carry-forward")
    summary = run_experiment(_config(tmp_path, snapshot, trials=2))
    stab = summary.stability["case-b"]
    # 第一遍干净 → pass；第二遍被第一遍的写污染 → fail —— 翻转被系统检出
    assert [t["taskSuccess"] for t in stab["trials"]] == [True, False]
    assert stab["label"] == "flip"
    assert stab["verdict"] == "fail"  # flaky 不允许 pass


def test_reset_policy_isolates_the_same_defect(tmp_path):
    """同一 Case 在 reset 下每遍都干净 → stable-green：证明缺陷是状态依赖的。"""
    snapshot = _snapshot(tmp_path)
    _clean_state_oracle(snapshot, "reset")
    summary = run_experiment(_config(tmp_path, snapshot, trials=2))
    stab = summary.stability["case-b"]
    assert stab["label"] == "stable-green"
    assert all(t["taskSuccess"] for t in stab["trials"])


def test_policy_name_parsing():
    from types import SimpleNamespace
    assert policy_name_of(
        SimpleNamespace(environment={"statePolicy": {"policy": "carry-forward"}})
    ) == "carry-forward"
    assert policy_name_of(
        SimpleNamespace(environment={"statePolicy": {"reset": "per-trial"}})
    ) == "reset"
    assert policy_name_of(SimpleNamespace(environment=None)) == "reset"


# ── 4) 全 pass 但关键指标不同 → drifting ─────────────────────────


def test_drifting_when_scores_differ():
    label = stability_label([
        {"taskSuccess": True, "score": 100, "invalid": False},
        {"taskSuccess": True, "score": 90, "invalid": False},
    ])
    assert label == "drifting"
    assert stability_layer("x", label).verdict == "fail"  # 漂移不满足可复现


# ── 5) 认证失效 → invalid，不得错误产生 stable-red ───────────────


def test_invalid_trial_is_not_stable_red(tmp_path):
    snapshot = _snapshot(tmp_path)
    real = ADAPTERS["maa"]

    class AuthBlocked:
        name = "maa"

        @staticmethod
        def preflight(case, variant):
            return PreflightResult(ok=False, reason="认证令牌过期",
                                   blocked_kind="auth")

        @staticmethod
        def prepare(case, workspace):
            return real.prepare(case, workspace)

        @staticmethod
        def run(prepared):
            raise AssertionError("认证阻断后不应执行")

        @staticmethod
        def collect(prepared):
            return real.collect(prepared)

        @staticmethod
        def cleanup(prepared, outcome):
            return real.cleanup(prepared, outcome)

    ADAPTERS["maa"] = AuthBlocked()
    try:
        summary = run_experiment(_config(tmp_path, snapshot, trials=2))
    finally:
        ADAPTERS["maa"] = real
    stab = summary.stability["case-a"]
    assert stab["label"] == "invalid"          # 不是 stable-red
    assert stab["verdict"] == "inconclusive"   # 也不算业务失败
    assert all(t["invalid"] for t in stab["trials"])


def test_label_mapping_unit():
    assert stability_label([{"taskSuccess": True, "score": 1, "invalid": False},
                           {"taskSuccess": True, "score": 1, "invalid": False}]) \
        == "stable-green"
    assert stability_label([{"taskSuccess": False, "score": 0, "invalid": False},
                           {"taskSuccess": False, "score": 0, "invalid": False}]) \
        == "stable-red"
    assert stability_label([{"taskSuccess": True, "score": 1, "invalid": False},
                           {"taskSuccess": False, "score": 0, "invalid": False}]) \
        == "flip"
    assert stability_label([{"taskSuccess": False, "score": 0, "invalid": True},
                            {"taskSuccess": True, "score": 1, "invalid": False}]) \
        == "invalid"  # invalid 优先于任何业务标签


# ── 6) 未声明隔离的写 Case 不并发 ────────────────────────────────


def test_write_lock_double_acquire_raises():
    locks = WriteLockRegistry()
    locks.acquire(["policy"], "case-a")
    with pytest.raises(WriteLockError, match="policy"):
        locks.acquire(["policy"], "case-b")
    locks.release(["policy"])
    locks.acquire(["policy"], "case-b")  # 释放后可再获取
    locks.release(["policy"])


def test_write_cases_serialize_and_order_deterministic(tmp_path):
    snapshot = _snapshot(tmp_path)
    # 加一个与 case-b 写同一目标的 Case（未声明隔离）
    (snapshot / "case-d").mkdir()
    shutil.copy2(snapshot / "case-b" / "trace.json", snapshot / "case-d" / "trace.json")
    shutil.copy2(snapshot / "case-b" / "recording.json",
                 snapshot / "case-d" / "recording.json")
    value = _edit_dataset(snapshot)
    value["cases"].append({
        "id": "case-d", "executor": "web",
        "input": {"trace": "case-d/trace.json",
                  "recording": "case-d/recording.json"},
        "environment": {"sideEffects": ["write:policy"],
                      "statePolicy": {"policy": "reset"}}})
    _save_dataset(snapshot, value)
    build_manifest(snapshot)

    events: list = []
    real = ADAPTERS["web"]

    class SerialSpy:
        name = "web"

        @staticmethod
        def preflight(case, variant):
            return real.preflight(case, variant)

        @staticmethod
        def prepare(case, workspace):
            return real.prepare(case, workspace)

        @staticmethod
        def run(prepared):
            events.append(f"{prepared.case_id}:start")
            outcome = real.run(prepared)
            events.append(f"{prepared.case_id}:end")
            return outcome

        @staticmethod
        def collect(prepared):
            return real.collect(prepared)

        @staticmethod
        def cleanup(prepared, outcome):
            return real.cleanup(prepared, outcome)

    ADAPTERS["web"] = SerialSpy()
    try:
        summary = run_experiment(_config(tmp_path, snapshot, trials=1))
    finally:
        ADAPTERS["web"] = real

    # 写同一目标：case-b 完全结束后 case-d 才开始（不并发）
    assert events == ["case-b:start", "case-b:end",
                      "case-d:start", "case-d:end"]
    # 确定性顺序：case ID 排序
    assert [d.name.split("__")[0] for d in summary.run_dirs] == [
        "case-a", "case-b", "case-d"]


# ── 每轮初始状态摘要 + cleanup 结果随 Run 保存 ────────────────────


def test_initial_state_summary_and_cleanup_saved(tmp_path):
    snapshot = _snapshot(tmp_path)
    _clean_state_oracle(snapshot, "carry-forward")
    summary = run_experiment(_config(tmp_path, snapshot, trials=2))
    store = ExperimentStore(tmp_path / "store")
    case_b_runs = sorted(d for d in summary.run_dirs
                         if d.name.startswith("case-b"))
    first = store.load_run(case_b_runs[0])
    second = store.load_run(case_b_runs[1])
    assert first.environment["statePolicy"] == "carry-forward"
    assert first.trial == 1 and second.trial == 2
    summary0 = first.extra["initialStateSummary"]
    assert summary0["digest"].startswith("sha256:")
    assert summary0["state"] == {}  # 第一遍从干净状态开始
    assert first.extra["cleanup"]["ok"] is True
    # 状态流转可见：第二遍的初始状态已含第一遍的写
    summary1 = second.extra["initialStateSummary"]
    assert summary1["state"] == {"writes": {"policy": 1}}
    assert second.extra["stateAfter"]["applied"] == ["policy"]


# ── 退出条件：稳定性来自统一 Experiment ───────────────────────────


def test_stability_persisted_with_experiment(tmp_path):
    snapshot = _snapshot(tmp_path)
    summary = run_experiment(_config(tmp_path, snapshot, trials=2))
    path = summary.experiment_dir / "stability.jsonl"
    assert path.exists()
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert {r["caseId"] for r in records} == {"case-a", "case-b"}
    for record in records:
        assert record["label"] in ("stable-green", "stable-red", "flip",
                                   "drifting", "invalid")
        assert len(record["trials"]) == 2
