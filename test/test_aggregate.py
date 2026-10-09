"""Round 6 验收：聚合、Baseline 比较与回归分析。

逐项守设计文档 Round 6 验收标准：

1. 同一 Dataset 版本才能直接做严格 pairwise；
2. Dataset 不同时必须报告 Case 增删，不能静默比较均值；
3. 聚合结果可由逐 Run 结果重新计算；
4. 任一总指标都能下钻到贡献 Case；
5. 报告优先列出新增失败，不用平均分掩盖少数严重退化；
6. Snapshot 测试保证历史 Experiment 不随当前代码重新解释。
退出条件：能回答「Candidate 相对 Baseline 新坏了什么、修好了什么」。
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from trace_eval.aggregate import (
    SCHEMA_COMPARISON, SCHEMA_SUMMARY, aggregate_experiment,
    compare_experiments, render_comparison_report, render_summary_report,
)
from trust.stable_json import dumps_stable

HERE = Path(__file__).resolve().parent / "fixtures" / "aggregate"
REPO = HERE.parents[2]


def _frozen(name: str) -> dict:
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def _stable(value: dict) -> str:
    return dumps_stable(value) + "\n"


# ── 6) 快照：历史 Experiment 不被当前代码重新解释 ────────────────


def test_snapshot_summaries_recompute_identical():
    for exp, frozen in (("experiment-baseline", "summary-baseline.json"),
                        ("experiment-candidate", "summary-candidate.json")):
        recomputed = aggregate_experiment(HERE / exp)
        assert recomputed["schema"] == SCHEMA_SUMMARY
        assert _stable(recomputed) == (HERE / frozen).read_text(encoding="utf-8"), \
            f"{exp} 的聚合结果与冻结快照不符 —— 聚合语义被重新解释了"


def test_snapshot_comparisons_recompute_identical():
    for name, (a, b) in {
        "comparison-baseline-candidate.json":
            ("experiment-baseline", "experiment-candidate"),
        "comparison-baseline-other.json":
            ("experiment-baseline", "experiment-other"),
    }.items():
        recomputed = compare_experiments(HERE / a, HERE / b)
        assert recomputed["schema"] == SCHEMA_COMPARISON
        assert _stable(recomputed) == (HERE / name).read_text(encoding="utf-8"), \
            f"{name} 与冻结快照不符 —— 比较语义被重新解释了"


# ── 1) 严格 pairwise 只在同一 Dataset 版本 ───────────────────────


def test_strict_pairwise_same_dataset_only():
    strict = compare_experiments(HERE / "experiment-baseline",
                                HERE / "experiment-candidate")
    assert strict["mode"] == "strict"
    assert strict["meanComparison"] == "允许"
    diff = compare_experiments(HERE / "experiment-baseline",
                               HERE / "experiment-other")
    assert diff["mode"] == "dataset-diff"
    # 非严格模式：只有共有 Case 可比（case 级，且增删已显式报告）——
    # 禁止的是「静默比较均值」，不是禁止比较共有 Case
    assert list(diff["pairwise"]) == ["case-a"]
    assert "拒绝" in diff["meanComparison"]


# ── 2) Dataset 不同：报告增删，拒绝比较均值 ───────────────────────


def test_dataset_diff_reports_additions_removals_refuses_means():
    diff = _frozen("comparison-baseline-other.json")
    assert diff["added"] == ["case-e"]
    assert diff["removed"] == ["case-b", "case-c", "case-d"]
    assert "拒绝" in diff["meanComparison"]
    report = render_comparison_report(diff)
    assert "Case 增删" in report and "case-e" in report


# ── 退出条件：新坏了什么、修好了什么（含漂移/无效分列）──────────


def test_pairwise_answers_what_broke_and_what_fixed():
    comparison = _frozen("comparison-baseline-candidate.json")
    assert comparison["headline"]["newFailure"] == ["case-a"]
    assert comparison["headline"]["fixed"] == ["case-b"]
    # 漂移不是「新增失败」—— 每遍仍成功但关键指标变了，单列
    assert comparison["headline"]["drifting"] == ["case-c"]
    assert comparison["headline"]["invalid"] == ["case-d"]
    entry = comparison["pairwise"]["case-a"]
    assert entry["status"] == "new-failure"
    assert entry["candidate"]["failureCodes"] == ["ORACLE_API_STATE_WRONG"]


# ── 3) 聚合结果可由逐 Run 结果重新计算 ───────────────────────────


def test_aggregate_recomputes_from_runs_after_mutation(tmp_path):
    src = HERE / "experiment-baseline"
    work = tmp_path / "exp"
    shutil.copytree(src, work)
    # 把 case-b 的 oracle fail 改成 pass —— 聚合结论必须跟着变
    results = next((work / "runs").glob("case-b*/results.jsonl"))
    lines = [json.loads(line) for line in results.read_text().splitlines()]
    for line in lines:
        if line["key"].startswith("oracle."):
            line["verdict"] = "pass"
            line["failureCode"] = None
            line["evidence"] = None
            line["comment"] = "mutated"
    results.write_text(
        "\n".join(json.dumps(l, ensure_ascii=False, sort_keys=True)
                  for l in lines) + "\n", encoding="utf-8")

    summary = aggregate_experiment(work)
    assert summary["cases"]["case-b"]["verdict"] == "pass"
    assert summary["cases"]["case-b"]["failureCodes"] == []
    assert summary["totals"]["byVerdict"]["pass"] == 3  # a、b、c
    # 未改动的 Case 结论不变（证明重算只依赖逐 Run）
    frozen = _frozen("summary-baseline.json")
    assert summary["cases"]["case-a"] == frozen["cases"]["case-a"]


# ── 4) 任一总指标都能下钻到贡献 Case ─────────────────────────────


def test_every_metric_drills_down_to_cases():
    summary = _frozen("summary-baseline.json")
    all_cases = set(summary["cases"])
    for slice_name, slice_value in summary["slices"].items():
        assert slice_value, f"切片 {slice_name} 为空却仍出现在输出里"
        for key, entry in slice_value.items():
            assert isinstance(entry["cases"], list) and entry["cases"], \
                f"{slice_name}[{key}] 缺少可下钻的贡献 Case"
            assert set(entry["cases"]) <= all_cases
    # executor/risk 两个互斥切片合起来必须覆盖全部 Case
    for partition in ("byExecutor", "byRisk"):
        covered = set()
        for entry in summary["slices"][partition].values():
            covered.update(entry["cases"])
        assert covered == all_cases, f"{partition} 未覆盖全部 Case"
    # Case 级指标能继续下钻到 run
    case_a = summary["cases"]["case-a"]
    assert case_a["runIds"] and case_a["trials"] == len(case_a["runIds"])


# ── 5) 报告优先列新增失败，平均分只作参考 ────────────────────────


def test_comparison_report_lists_new_failures_first():
    comparison = _frozen("comparison-baseline-candidate.json")
    report = render_comparison_report(comparison)
    i_new = report.index("新增失败")
    i_fixed = report.index("已修复")
    i_drift = report.index("漂移")
    i_ref = report.index("参考指标")
    assert i_new < i_fixed < i_drift < i_ref
    assert "case-a" in report[i_new:i_fixed]  # 新增失败出现在最前区块
    # 平均分只出现在参考区，且明确「不用于判断好坏」
    ref = report[i_ref:]
    assert "平均" in ref and "不用于判断好坏" in report
    assert "平均" not in report[:i_ref]


def test_summary_report_lists_failing_cases_first():
    summary = _frozen("summary-baseline.json")
    report = render_summary_report(summary)
    i_fail = report.index("失败 Case")
    i_slice = report.index("切片")
    assert i_fail < i_slice
    assert "case-b" in report[i_fail:i_slice]  # baseline 唯一业务失败
    assert "case-d" in report[i_fail:i_slice]  # invalid 也要可见


# ── CLI：aggregate / compare 子命令 ───────────────────────────────


def _run_cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "trace_eval.aggregate", *argv],
        cwd=REPO, capture_output=True, text=True, timeout=60)


def test_cli_aggregate_matches_frozen(tmp_path):
    out = _run_cli("aggregate", str(HERE / "experiment-baseline"),
                   "--out", str(tmp_path))
    assert out.returncode == 0, out.stderr
    produced = json.loads((tmp_path / "summary.json").read_text())
    assert _stable(produced) == (HERE / "summary-baseline.json").read_text()
    assert (tmp_path / "summary.report.md").exists()


def test_cli_compare_matches_frozen(tmp_path):
    out = _run_cli("compare", str(HERE / "experiment-baseline"),
                   str(HERE / "experiment-candidate"), "--out", str(tmp_path))
    assert out.returncode == 0, out.stderr
    produced = json.loads((tmp_path / "comparison.json").read_text())
    assert _stable(produced) == \
        (HERE / "comparison-baseline-candidate.json").read_text()
    assert (tmp_path / "comparison.report.md").exists()
    assert "mode=strict" in out.stdout


def test_cli_compare_rejects_non_experiment_dir(tmp_path):
    out = _run_cli("compare", str(tmp_path / "nope"),
                   str(HERE / "experiment-candidate"))
    assert out.returncode == 2
    assert "error" in out.stdout or "error" in out.stderr
