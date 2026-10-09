"""Round 7 端到端 smoke：固定 Dataset → 回放 → Gate → 发布决策。

验收对应：

- 新增 Case 失败时 CI 返回 1；
- 配置错误 / Artifact 缺失 / 基础设施错误返回 2；
- ``inconclusive`` 默认阻断，除非 policy 明确放行；
- Gate 报告包含命中的规则与证据，不只显示「未通过」；
- 现有测试保持通过 + 本端到端 smoke 测试；
- 旧 CLI 在兼容期内仍产生与 Round 0 一致的核心结果。
退出条件：团队可以用固定 Dataset 和 Gate 做重复、可解释的发布判断。
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from trace_eval.adapters import ADAPTERS, PreflightResult
from trace_eval.datasets import build_manifest
from trace_eval.gates import (
    GatePolicy, evaluate_gate, render_gate_report,
)
from trace_eval.runner import RunConfig, run_experiment
from trust.audit import audit
from trust.stable_json import dumps_stable

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "test" / "fixtures" / "contracts"
DATASET = FIXTURE / "dataset"
OLD_CLI_GOLDEN = FIXTURE / "golden-results"


def _snapshot(tmp_path: Path) -> Path:
    target = tmp_path / "snapshot"
    shutil.copytree(DATASET, target)
    return target


def _config(tmp_path: Path, snapshot: Path, exp_id: str) -> RunConfig:
    return RunConfig(snapshot_dir=snapshot, experiment_id=exp_id,
                     store_root=tmp_path / "store", trials=2)


def _edit_dataset(snapshot: Path) -> dict:
    return json.loads((snapshot / "dataset.json").read_text(encoding="utf-8"))


def _save_dataset(snapshot: Path, value: dict) -> None:
    (snapshot / "dataset.json").write_text(
        json.dumps(value, ensure_ascii=False), encoding="utf-8")


# ── CI 脚本：固定 Dataset 上的绿色路径（exit 0）──────────────────


def test_ci_smoke_script_passes_on_fixed_dataset():
    out = subprocess.run(["bash", str(REPO / "ci" / "smoke.sh")],
                         cwd=REPO, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "gate: PASS" in out.stdout


# ── 新增 Case 失败 → CI 返回 1 ────────────────────────────────────


def test_new_case_failure_blocks_ci_with_exit_1(tmp_path):
    # baseline：原 Dataset
    base_snapshot = _snapshot(tmp_path)
    run_experiment(_config(tmp_path, base_snapshot, "e2e-baseline"))
    # candidate：case-a 的 execution 任务失败（篡改 + 重建 manifest）
    cand_snapshot = _snapshot(tmp_path / "cand")
    execution = json.loads(
        (cand_snapshot / "case-a" / "execution.json").read_text())
    execution["status"] = "failed"
    (cand_snapshot / "case-a" / "execution.json").write_text(
        json.dumps(execution), encoding="utf-8")
    build_manifest(cand_snapshot)
    run_experiment(_config(tmp_path / "cand", cand_snapshot, "e2e-candidate"))

    baseline_dir = tmp_path / "store" / "e2e-baseline"
    candidate_dir = tmp_path / "cand" / "store" / "e2e-candidate"
    gate = evaluate_gate(candidate_dir, baseline_dir=baseline_dir)
    assert gate["exitCode"] == 1
    new_failure = next(h for h in gate["hits"] if h["rule"] == "new-failure")
    assert new_failure["cases"] == ["case-a"]
    assert "TASK_NOT_SUCCESSFUL" in \
        new_failure["evidence"]["case-a"]["failureCodes"]

    # CLI 的退出码必须与判定一致
    out = subprocess.run(
        [sys.executable, "-m", "trace_eval.gates", "check",
         str(candidate_dir), "--baseline", str(baseline_dir),
         "--out", str(tmp_path / "gate")],
        cwd=REPO, capture_output=True, text=True, timeout=60)
    assert out.returncode == 1, out.stdout + out.stderr
    assert (tmp_path / "gate" / "gate.json").exists()


# ── inconclusive 默认阻断；policy 明确放行才降级 ───────────────────


def _auth_blocked_adapter():
    real = ADAPTERS["maa"]

    class AuthBlocked:
        name = "maa"

        @staticmethod
        def preflight(case, variant):
            return PreflightResult(ok=False, reason="会话过期",
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

    return AuthBlocked


def test_inconclusive_blocks_by_default(tmp_path):
    snapshot = _snapshot(tmp_path)
    ADAPTERS["maa"] = _auth_blocked_adapter()
    try:
        run_experiment(_config(tmp_path, snapshot, "e2e-inconclusive"))
    finally:
        _restore_maa()
    gate = evaluate_gate(tmp_path / "store" / "e2e-inconclusive")
    assert gate["exitCode"] == 1
    inconclusive = next(h for h in gate["hits"]
                        if h["rule"] == "inconclusive")
    assert inconclusive["action"] == "block"
    assert "case-a" in inconclusive["cases"]


def test_policy_can_explicitly_allow_inconclusive(tmp_path):
    snapshot = _snapshot(tmp_path)
    ADAPTERS["maa"] = _auth_blocked_adapter()
    try:
        run_experiment(_config(tmp_path, snapshot, "e2e-policy"))
    finally:
        _restore_maa()
    gate = evaluate_gate(
        tmp_path / "store" / "e2e-policy",
        policy=GatePolicy.from_dict({
            "schema": "trace-evals.gate-policy/v1",
            "blockOnInconclusive": False,
        }))
    assert gate["exitCode"] == 0
    inconclusive = next(h for h in gate["hits"]
                        if h["rule"] == "inconclusive")
    assert inconclusive["action"] == "warn"  # 记录了，但不阻断


# import 时捕获（早于任何测试替换）—— 旧 CLI 兼容期内的原始 Adapter
_MAA_ORIGINAL = ADAPTERS["maa"]


def _restore_maa():
    ADAPTERS["maa"] = _MAA_ORIGINAL


# ── 配置错误 / 基础设施错误 → 2 ──────────────────────────────────


def _gate_cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "trace_eval.gates", "check", *argv],
        cwd=REPO, capture_output=True, text=True, timeout=60)


def test_config_errors_return_2(tmp_path):
    # 不存在的 Experiment 目录
    out = _gate_cli(str(tmp_path / "nope"))
    assert out.returncode == 2
    # 声明的 Baseline 缺失
    exp = tmp_path / "store" / "x"
    out = _gate_cli(str(exp), "--baseline", str(tmp_path / "no-baseline"))
    assert out.returncode == 2
    # policy 非法 JSON
    bad = tmp_path / "policy.json"
    bad.write_text("{not json", encoding="utf-8")
    out = _gate_cli(str(exp), "--policy", str(bad))
    assert out.returncode == 2
    # policy 未知键
    bad2 = tmp_path / "policy2.json"
    bad2.write_text('{"schema": "trace-evals.gate-policy/v1", "bogus": true}',
                    encoding="utf-8")
    out = _gate_cli(str(exp), "--policy", str(bad2))
    assert out.returncode == 2


# ── Gate 报告含命中规则与证据 ────────────────────────────────────


def test_gate_report_contains_rule_and_evidence(tmp_path):
    base_snapshot = _snapshot(tmp_path)
    run_experiment(_config(tmp_path, base_snapshot, "rep-base"))
    cand_snapshot = _snapshot(tmp_path / "cand")
    execution = json.loads(
        (cand_snapshot / "case-a" / "execution.json").read_text())
    execution["status"] = "failed"
    (cand_snapshot / "case-a" / "execution.json").write_text(
        json.dumps(execution), encoding="utf-8")
    build_manifest(cand_snapshot)
    run_experiment(_config(tmp_path / "cand", cand_snapshot, "rep-cand"))

    candidate_dir = tmp_path / "cand" / "store" / "rep-cand"
    gate = evaluate_gate(candidate_dir,
                         baseline_dir=tmp_path / "store" / "rep-base")
    report = render_gate_report(gate)
    assert "命中规则" in report
    assert "`new-failure`" in report
    assert "case-a" in report
    assert "TASK_NOT_SUCCESSFUL" in report       # 证据：failureCode
    assert "EXECUTION_TASK" in report or "stable-red" in report  # 证据：label


# ── 旧 CLI 兼容期：核心结果与 Round 0 基线一致 ────────────────────


@pytest.mark.parametrize("case,golden", [
    ("case-success", "audit-case-success.json"),
    ("case-weak", "audit-case-weak.json"),
])
def test_old_cli_core_results_match_round0_baseline(case, golden):
    result = audit(FIXTURE / "cases" / case)
    frozen = (OLD_CLI_GOLDEN / golden).read_text(encoding="utf-8")
    assert dumps_stable(result) + "\n" == frozen, \
        f"旧 CLI（trust/audit）核心结果偏离 Round 0 基线：{case}"
