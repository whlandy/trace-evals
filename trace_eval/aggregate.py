#!/usr/bin/env python3
"""Round 6：聚合、Baseline/Candidate 比较与回归分析。

目标（设计 Round 6）：把逐 Run 结果变成可用于工程决策的 Experiment 对比。

- **聚合**（``aggregate_experiment``）：从 Experiment 目录的逐 Run 结果
  **重新计算**（不信任任何缓存结论；同一输入必得同一输出）——
  Case / tag / executor / risk / failure code 五个切片，任一总指标都能
  下钻到贡献 Case（切片条目必带 ``cases`` 列表）。
- **pairwise**（``compare_experiments``）：Baseline/Candidate 逐 Case 比较，
  新增失败、修复、漂移、无效**分别列出**。只有**同一 Dataset 版本**
  （digest 相同且 Case 集合相同）才允许严格 pairwise；Dataset 不同时
  必须报告 Case 增删，**拒绝比较均值**（不能静默比较）。
- **快照纪律**：``summary.json`` / ``comparison.json`` 是纯确定性输出
  （无时间戳）——历史 Experiment 的冻结文件即快照测试基准，
  当前代码若「重新解释」历史结果，快照测试必挂。
- 报告（``report.md``）**优先列出新增失败**，平均分数只作参考指标
  放在末尾——不用平均分掩盖少数严重退化。

CLI：

    python3 -m trace_eval.aggregate aggregate <experiment-dir>
    python3 -m trace_eval.aggregate compare <baseline-dir> <candidate-dir>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trace_eval.aggregation import stability_label
from trace_eval.contracts import parse_eval_run, parse_evaluator_result
from trust.stable_json import dumps_stable

SCHEMA_SUMMARY = "trace-evals.summary/v1"
SCHEMA_COMPARISON = "trace-evals.comparison/v1"

# 稳定性标签 → Case 级 verdict（与 C5 门语义一致）
CASE_VERDICT_BY_LABEL = {
    "stable-green": "pass",
    "stable-red": "fail",
    "flip": "fail",
    "drifting": "fail",
    "invalid": "inconclusive",
}


class AggregateError(RuntimeError):
    """Experiment 目录不可读/结构不完整。"""


# ── 读取：只从逐 Run 结果重算 ────────────────────────────────────


def trial_verdict(run, results: list) -> dict:
    """一个 trial → 标签输入（与 replay_lab 的 run 记录同形）。

    invalid / infra_error / cancelled 记 invalid：环境失效不是业务失败，
    当红标签就是在伪造真值。
    """
    if run.status in ("invalid", "infra_error", "cancelled"):
        return {"taskSuccess": False, "score": 0, "invalid": True}
    business_fail = any(r.verdict == "fail" for r in results)
    ok = run.status == "completed" and not business_fail
    score = next((r.score for r in results
                  if r.key.startswith("execution.")
                  and isinstance(r.score, (int, float))), 0)
    return {"taskSuccess": ok, "score": score, "invalid": False}


def load_experiment(experiment_dir: Path) -> dict:
    """读 Experiment 目录 → {meta, cases}。结论全部从逐 Run 重算。"""
    experiment_dir = Path(experiment_dir)
    meta_path = experiment_dir / "experiment.json"
    if not meta_path.exists():
        raise AggregateError(f"不是 Experiment 目录（缺 experiment.json）：{experiment_dir}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["cases"] = (meta.get("config") or {}).get("cases") or meta.get("cases") or []

    cases: dict[str, dict] = {}
    runs_dir = experiment_dir / "runs"
    if runs_dir.exists():
        for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
            final = run_dir / "run.json"
            if not final.exists():
                continue  # 半份 Run = 不存在（Round 3 原子性规则）
            try:
                run = parse_eval_run(json.loads(final.read_text(encoding="utf-8")))
            except Exception as error:  # noqa: BLE001
                raise AggregateError(f"run.json 损坏 {run_dir}：{error}") from error
            results = []
            results_path = run_dir / "results.jsonl"
            if results_path.exists():
                for line in results_path.read_text(encoding="utf-8").splitlines():
                    if line:
                        results.append(parse_evaluator_result(json.loads(line)))
            entry = cases.setdefault(run.case_id, {
                "case_id": run.case_id,
                "executor": next((c.get("executor") for c in meta["cases"]
                                  if c.get("id") == run.case_id), None),
                "tags": next((list(c.get("tags") or []) for c in meta["cases"]
                              if c.get("id") == run.case_id), []),
                "risk": next((c.get("risk") for c in meta["cases"]
                              if c.get("id") == run.case_id), None),
                "runs": [], "trial_verdicts": [], "results": [],
                "failure_codes": set(),
            })
            entry["runs"].append({
                "run_id": run.run_id, "trial": run.trial, "status": run.status,
                "score": None,
            })
            verdict = trial_verdict(run, results)
            entry["trial_verdicts"].append(verdict)
            entry["results"].extend(results)
            for r in results:
                if r.verdict == "fail" and r.failure_code:
                    entry["failure_codes"].add(r.failure_code)

    for entry in cases.values():
        # run 目录按名排序 ⇒ 同 Case 的 trial 已按 tNN 升序（new_run_id 格式保证）
        verdicts = entry["trial_verdicts"]
        entry["label"] = stability_label(verdicts) if verdicts else "invalid"
        entry["verdict"] = CASE_VERDICT_BY_LABEL[entry["label"]]
        entry["scores"] = [v["score"] for v in entry["trial_verdicts"]]
        entry["failure_codes"] = sorted(entry["failure_codes"])
    return {"meta": meta, "cases": cases}


# ── 聚合（五切片，均可下钻）──────────────────────────────────────


def _slice_entry(case_ids: list[str]) -> dict:
    """切片条目：计数 + 贡献 Case 列表（下钻入口）。"""
    verdicts = {}
    for case_id in case_ids:
        verdicts[case_id] = None  # 占位防重复键
    return {"cases": sorted(case_ids), "caseCount": len(set(case_ids))}


def aggregate_experiment(experiment_dir: Path) -> dict:
    """逐 Run 结果 → 确定性聚合（无时间戳；同一输入必得同一输出）。"""
    data = load_experiment(experiment_dir)
    meta = data["meta"]
    cases = data["cases"]

    cases_out = {}
    for case_id, entry in sorted(cases.items()):
        cases_out[case_id] = {
            "verdict": entry["verdict"],
            "stabilityLabel": entry["label"],
            "scores": entry["scores"],
            "failureCodes": entry["failure_codes"],
            "runIds": [r["run_id"] for r in entry["runs"]],
            "trials": len(entry["trial_verdicts"]),
            "executor": entry["executor"],
            "tags": entry["tags"],
        }

    # 切片：Case / tag / executor / risk / failure code —— 每条都带贡献 Case
    by_tag: dict = {}
    by_executor: dict = {}
    by_risk: dict = {}
    for case_id, entry in sorted(cases.items()):
        for tag in (entry["tags"] or ["(untagged)"]):
            by_tag.setdefault(tag, []).append(case_id)
        by_executor.setdefault(entry["executor"] or "(unknown)", []).append(case_id)
        by_risk.setdefault(entry["risk"] or "undeclared", []).append(case_id)
    by_failure_code: dict = {}
    for case_id, entry in sorted(cases.items()):
        for code in entry["failure_codes"]:
            by_failure_code.setdefault(code, []).append(case_id)

    verdicts_total: dict = {}
    for entry in cases.values():
        verdicts_total[entry["verdict"]] = verdicts_total.get(entry["verdict"], 0) + 1

    return {
        "schema": SCHEMA_SUMMARY,
        "experimentId": meta.get("id"),
        "datasetDigest": meta.get("datasetDigest"),
        "totals": {
            "cases": len(cases),
            "runs": sum(len(e["runs"]) for e in cases.values()),
            "byVerdict": verdicts_total,
        },
        "cases": cases_out,
        "slices": {
            "byTag": {k: _slice_entry(v) for k, v in sorted(by_tag.items())},
            "byExecutor": {k: _slice_entry(v) for k, v in sorted(by_executor.items())},
            "byRisk": {k: _slice_entry(v) for k, v in sorted(by_risk.items())},
            "byFailureCode": {k: _slice_entry(v) for k, v in sorted(by_failure_code.items())},
        },
    }


# ── Baseline/Candidate pairwise ──────────────────────────────────


def _pairwise_status(b: dict, c: dict) -> str:
    """逐 Case 的迁移状态（新增失败 / 修复 / 漂移 / 无效 / 不变）。"""
    if b["label"] == "invalid" or c["label"] == "invalid":
        return "invalid"
    b_pass = b["verdict"] == "pass"
    c_pass = c["verdict"] == "pass"
    if b_pass and not c_pass:
        # 每遍仍成功但关键指标漂移 —— 是「漂移」，不是「新增失败」
        # （不能把漂移归成失败，否则严重退化被失败率掩盖）
        return "drifting" if c["label"] == "drifting" else "new-failure"
    if c_pass and not b_pass:
        return "fixed"
    if b_pass and c_pass:
        if b["label"] != c["label"] or b["scores"] != c["scores"]:
            return "drifting"
        return "unchanged-pass"
    return "unchanged-fail"


def compare_experiments(baseline_dir: Path, candidate_dir: Path) -> dict:
    b = load_experiment(baseline_dir)
    c = load_experiment(candidate_dir)
    b_id, c_id = b["meta"].get("id"), c["meta"].get("id")
    b_digest, c_digest = b["meta"].get("datasetDigest"), c["meta"].get("datasetDigest")
    b_ids, c_ids = set(b["cases"]), set(c["cases"])
    common = sorted(b_ids & c_ids)
    added = sorted(c_ids - b_ids)
    removed = sorted(b_ids - c_ids)

    strict = b_digest == c_digest and not added and not removed
    pairwise = {}
    for case_id in common:
        be, ce = b["cases"][case_id], c["cases"][case_id]
        pairwise[case_id] = {
            "status": _pairwise_status(be, ce),
            "baseline": {"verdict": be["verdict"], "label": be["label"],
                          "scores": be["scores"],
                          "failureCodes": be["failure_codes"]},
            "candidate": {"verdict": ce["verdict"], "label": ce["label"],
                           "scores": ce["scores"],
                           "failureCodes": ce["failure_codes"]},
        }

    headline = {
        "newFailure": sorted(k for k, v in pairwise.items()
                             if v["status"] == "new-failure"),
        "fixed": sorted(k for k, v in pairwise.items()
                        if v["status"] == "fixed"),
        "drifting": sorted(k for k, v in pairwise.items()
                           if v["status"] == "drifting"),
        "invalid": sorted(k for k, v in pairwise.items()
                           if v["status"] == "invalid"),
    }
    return {
        "schema": SCHEMA_COMPARISON,
        "mode": "strict" if strict else "dataset-diff",
        "baseline": {"experimentId": b_id, "datasetDigest": b_digest},
        "candidate": {"experimentId": c_id, "datasetDigest": c_digest},
        "added": added,
        "removed": removed,
        "meanComparison": ("允许" if strict
                           else "拒绝：Dataset 版本不同，不能静默比较均值"),
        "headline": headline,
        "pairwise": pairwise,
    }


# ── 报告 ─────────────────────────────────────────────────────────


def _case_detail(comparison: dict, case_id: str) -> str:
    entry = comparison["pairwise"][case_id]
    cand = entry["candidate"]
    codes = ",".join(cand["failureCodes"]) or "-"
    return f"{case_id}（{cand['label']}，failureCodes: {codes}）"


def render_comparison_report(comparison: dict) -> str:
    """新增失败优先；平均分数只作参考。"""
    lines = [
        "# Experiment 比较报告",
        "",
        f"- mode: **{comparison['mode']}**",
        f"- baseline: {comparison['baseline']['experimentId']}"
        f"（dataset {comparison['baseline']['datasetDigest']}）",
        f"- candidate: {comparison['candidate']['experimentId']}"
        f"（dataset {comparison['candidate']['datasetDigest']}）",
        f"- 均值比较：{comparison['meanComparison']}",
        "",
    ]
    if comparison["added"]:
        lines += ["## Case 增删（Dataset 差异）", "",
                  f"- 新增：{', '.join(comparison['added'])}",
                  f"- 移除：{', '.join(comparison['removed'] or ['-'])}", ""]
    sections = (
        ("新增失败（优先处理）", "newFailure"),
        ("已修复", "fixed"),
        ("漂移", "drifting"),
        ("无效（环境/认证，非业务结论）", "invalid"),
    )
    for title, key in sections:
        members = comparison["headline"][key]
        lines.append(f"## {title}（{len(members)}）")
        if not members:
            lines.append("（无）")
        for case_id in members:
            lines.append(f"- {_case_detail(comparison, case_id)}")
        lines.append("")
    lines.append("## 参考指标（不用于判断好坏）")
    scores = [s for entry in comparison["pairwise"].values()
              for s in entry["candidate"]["scores"]]
    if scores:
        lines.append(f"- candidate 各 trial 分数：{scores}"
                     f"（平均 {sum(scores) / len(scores):.1f}，"
                     "仅供参考——不用平均值掩盖退化）")
    else:
        lines.append("- （无可比分数）")
    return "\n".join(lines) + "\n"


def render_summary_report(summary: dict) -> str:
    lines = [
        "# Experiment 汇总",
        "",
        f"- experiment: {summary['experimentId']}"
        f"（dataset {summary['datasetDigest']}）",
        f"- totals: {summary['totals']}",
        "",
        "## 失败 Case（优先列出）",
    ]
    failing = {k: v for k, v in summary["cases"].items()
               if v["verdict"] != "pass"}
    if not failing:
        lines.append("（无）")
    for case_id, entry in sorted(failing.items()):
        codes = ",".join(entry["failureCodes"]) or "-"
        lines.append(f"- {case_id}：verdict={entry['verdict']}"
                     f"，label={entry['stabilityLabel']}，"
                     f"failureCodes: {codes}")
    lines += ["", "## 切片（每条可下钻到贡献 Case）", ""]
    for slice_name, slice_value in summary["slices"].items():
        lines.append(f"### {slice_name}")
        for key, entry in slice_value.items():
            lines.append(f"- {key}: {entry['cases']}")
        lines.append("")
    scores = [s for e in summary["cases"].values() for s in e["scores"]]
    lines.append("## 参考指标")
    if scores:
        lines.append(f"- 各 trial 分数：{scores}（平均 {sum(scores) / len(scores):.1f}，"
                     "仅供参考）")
    return "\n".join(lines) + "\n"


# ── CLI ──────────────────────────────────────────────────────────


def _write_outputs(directory: Path, name: str, payload: dict,
                   report: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(
        dumps_stable(payload) + "\n", encoding="utf-8")
    (directory / f"{name}.report.md").write_text(report, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m trace_eval.aggregate")
    sub = parser.add_subparsers(dest="command", required=True)
    p_agg = sub.add_parser("aggregate", help="聚合一个 Experiment")
    p_agg.add_argument("experiment_dir")
    p_agg.add_argument("--out", type=Path, default=None)
    p_cmp = sub.add_parser("compare", help="Baseline/Candidate 比较")
    p_cmp.add_argument("baseline")
    p_cmp.add_argument("candidate")
    p_cmp.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        if args.command == "aggregate":
            experiment_dir = Path(args.experiment_dir)
            summary = aggregate_experiment(experiment_dir)
            out = args.out or experiment_dir
            _write_outputs(Path(out), "summary", summary,
                            render_summary_report(summary))
            print("summary.json + summary.report.md →", Path(out))
        else:
            comparison = compare_experiments(Path(args.baseline),
                                              Path(args.candidate))
            out = args.out or Path(args.candidate)
            _write_outputs(Path(out), "comparison", comparison,
                            render_comparison_report(comparison))
            print(f"mode={comparison['mode']}",
                  f"newFailure={comparison['headline']['newFailure']}",
                  f"fixed={comparison['headline']['fixed']}",
                  f"drifting={comparison['headline']['drifting']}",
                  "comparison.json + comparison.report.md →", out)
    except (AggregateError, ValueError) as error:
        print("error:", error)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
