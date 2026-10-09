#!/usr/bin/env python3
"""Round 7：CI Gate —— 把 Experiment 结果转成可审计的发布决策。

设计 Round 7 验收标准逐条对应：

- **新增 Case 失败 → CI 返回 1**：pairwise（有 Baseline）或 Case 级
  （无 Baseline）判定出业务失败 → 退出码 1；
- **配置错误 / Artifact 缺失 / 基础设施错误 → 返回 2**：policy 解析
  失败、Experiment 目录不可读、声明的 Baseline 缺失、run 状态
  ``infra_error`` → 退出码 2（这是「这次判定本身不可信」，
  与「产品坏了」的 1 区分）；
- **``inconclusive`` 默认阻断**：缺证据不是失败，但**也不能放行**
  —— 只有 policy 显式 ``blockOnInconclusive: false`` 才降级为 warn；
- **Gate 报告包含命中规则与证据**：每条 hit 带 rule / cases /
  证据（label、failureCode、run 状态），不是只显示「未通过」。

输出纯确定性（无时间戳）：同一 Experiment + policy 必得同一 Gate 结论。

CLI：

    python3 -m trace_eval.gates check <experiment-dir>
        [--baseline <baseline-experiment-dir>]
        [--policy <gate-policy.json>]
        [--out <dir>]

退出码：0 = 放行；1 = 阻断（新增失败 / inconclusive / 漂移）；
2 = 配置或基础设施错误（判定本身不可信）。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

from trace_eval.aggregate import (AggregateError, compare_experiments,
                                  load_experiment)
from trust.stable_json import dumps_stable

SCHEMA_GATE_POLICY = "trace-evals.gate-policy/v1"
SCHEMA_GATE = "trace-evals.gate/v1"

POLICY_KEYS = ("blockOnNewFailure", "blockOnInconclusive", "blockOnDrifting")


class GateConfigError(ValueError):
    """配置错误 / 判定不可信（退出码 2 的原因）。"""


# ── Gate Policy ──────────────────────────────────────────────────


@dataclass
class GatePolicy:
    """默认全阻断；只有显式 false 才放行（缺证据默认不放行）。"""
    block_on_new_failure: bool = True
    block_on_inconclusive: bool = True
    block_on_drifting: bool = True
    allow_failure_codes: list[str] = field(default_factory=list)

    @classmethod
    def default(cls) -> "GatePolicy":
        return cls()

    @classmethod
    def from_dict(cls, value: dict) -> "GatePolicy":
        schema = value.get("schema")
        if schema != SCHEMA_GATE_POLICY:
            raise GateConfigError(
                f"gate policy schema 必须为 {SCHEMA_GATE_POLICY!r}，"
                f"实际 {schema!r}")
        for key, raw in value.items():
            if key == "schema":
                continue
            if key == "allowFailureCodes":
                continue
            if key not in POLICY_KEYS:
                raise GateConfigError(f"未知 gate policy 键 {key!r}")
            if not isinstance(raw, bool):
                raise GateConfigError(f"gate policy.{key} 必须是布尔值")
        allow = value.get("allowFailureCodes") or []
        if not isinstance(allow, list) or not all(isinstance(x, str) for x in allow):
            raise GateConfigError("allowFailureCodes 必须是字符串数组")
        return cls(
            block_on_new_failure=value.get("blockOnNewFailure", True),
            block_on_inconclusive=value.get("blockOnInconclusive", True),
            block_on_drifting=value.get("blockOnDrifting", True),
            allow_failure_codes=list(allow),
        )

    @classmethod
    def load(cls, path: Path) -> "GatePolicy":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise GateConfigError(f"gate policy 不存在：{path}") from error
        except json.JSONDecodeError as error:
            raise GateConfigError(
                f"gate policy 不是合法 JSON：{error}") from error
        if not isinstance(raw, dict):
            raise GateConfigError("gate policy 必须是 JSON 对象")
        return cls.from_dict(raw)

    def to_dict(self) -> dict:
        out = {"schema": SCHEMA_GATE_POLICY}
        out["blockOnNewFailure"] = self.block_on_new_failure
        out["blockOnInconclusive"] = self.block_on_inconclusive
        out["blockOnDrifting"] = self.block_on_drifting
        if self.allow_failure_codes:
            out["allowFailureCodes"] = list(self.allow_failure_codes)
        return out


# ── Gate 判定 ─────────────────────────────────────────────────────


@dataclass
class GateHit:
    rule: str
    action: str  # block | warn
    cases: list
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"rule": self.rule, "action": self.action,
                "cases": self.cases, "evidence": self.evidence}


def _case_evidence(summary: dict, case_id: str) -> dict:
    entry = summary["cases"][case_id]
    return {
        "verdict": entry["verdict"],
        "stabilityLabel": entry["stabilityLabel"],
        "failureCodes": entry["failureCodes"],
        "runStatuses": entry["statuses"],
    }


def evaluate_gate(experiment_dir: Path,
                  baseline_dir: Path | None = None,
                  policy: GatePolicy | None = None) -> dict:
    """Experiment（+可选 Baseline）→ Gate 结论。

    规则：
      R-infra        任一 run 状态 infra_error → 判定不可信（exit 2）
      R-new-failure  pairwise 出现新增失败（Baseline 存在时）→ exit 1
      R-case-fail    无 Baseline 时 Case 级业务失败 → exit 1
      R-inconclusive 缺证据的 Case 默认阻断；policy 显式放行 → warn
      R-drifting     pairwise 漂移（policy 默认阻断）
    ``allowFailureCodes`` 命中的 failureCode 降级为 warn（显式放行）。
    """
    policy = policy or GatePolicy.default()
    summary = load_experiment(experiment_dir)
    experiment = summary["meta"]
    summary_view = {  # evaluate 内部用 Case 视图（与 aggregate 输出同形）
        "experimentId": experiment.get("id"),
        "datasetDigest": experiment.get("datasetDigest"),
        "cases": {
            case_id: {
                "verdict": entry["verdict"],
                "stabilityLabel": entry["label"],
                "scores": entry["scores"],
                "failureCodes": entry["failure_codes"],
                "runIds": [r["run_id"] for r in entry["runs"]],
                "statuses": [r["status"] for r in entry["runs"]],
                "executor": entry["executor"], "tags": entry["tags"],
            }
            for case_id, entry in summary["cases"].items()
        },
    }

    comparison = None
    if baseline_dir is not None:
        baseline_dir = Path(baseline_dir)
        if not (baseline_dir / "experiment.json").exists():
            raise GateConfigError(f"声明的 Baseline 不可读：{baseline_dir}")
        comparison = compare_experiments(baseline_dir, experiment_dir)

    hits: list[GateHit] = []
    exit_code = 0

    # R-infra：基础设施错误 —— 判定本身不可信（exit 2）
    infra_cases = sorted(
        case_id for case_id, entry in summary_view["cases"].items()
        if "infra_error" in entry["statuses"])
    if infra_cases:
        hits.append(GateHit("infra-error", "block", infra_cases,
                            {c: _case_evidence(summary_view, c)
                             for c in infra_cases}))
        exit_code = 2

    def _allowed(case_id: str) -> bool:
        """policy 显式放行该 Case 的失败码。"""
        codes = summary_view["cases"][case_id]["failureCodes"]
        return bool(codes) and \
            all(code in policy.allow_failure_codes for code in codes)

    # R-new-failure / R-case-fail：业务失败（exit 1）
    if comparison is not None:
        failed = list(comparison["headline"]["newFailure"])
        action = "block" if policy.block_on_new_failure else "warn"
        if failed and action == "block":
            exit_code = max(exit_code, 1)
        if failed:
            evidence = {}
            for case_id in failed:
                if comparison["pairwise"][case_id]["candidate"]["failureCodes"] \
                        and _allowed(case_id):
                    action_case = "warn"
                else:
                    action_case = action
                evidence[case_id] = {
                    **_case_evidence(summary_view, case_id),
                    "baseline": comparison["pairwise"][case_id]["baseline"],
                    "pairwiseStatus": "new-failure",
                }
                if action_case == "block" and action == "block":
                    exit_code = max(exit_code, 1)
            hits.append(GateHit("new-failure", action, failed, evidence))
    else:
        failed = sorted(case_id for case_id, entry in
                        summary_view["cases"].items()
                        if entry["verdict"] == "fail")
        if failed:
            evidence = {}
            for case_id in failed:
                action_case = "warn" if _allowed(case_id) else "block"
                evidence[case_id] = _case_evidence(summary_view, case_id)
                if action_case == "block":
                    exit_code = max(exit_code, 1)
            hits.append(GateHit("case-failure", "block"
                                if policy.block_on_new_failure else "warn",
                                failed, evidence))

    # R-inconclusive：缺证据默认阻断
    inconclusive = sorted(case_id for case_id, entry in
                          summary_view["cases"].items()
                          if entry["verdict"] == "inconclusive")
    if inconclusive:
        action = "block" if policy.block_on_inconclusive else "warn"
        if action == "block":
            exit_code = max(exit_code, 1)
        hits.append(GateHit("inconclusive", action, inconclusive,
                            {c: _case_evidence(summary_view, c)
                             for c in inconclusive}))

    # R-drifting：关键指标漂移（policy 默认阻断）
    if comparison is not None:
        drifting = list(comparison["headline"]["drifting"])
        if drifting:
            action = "block" if policy.block_on_drifting else "warn"
            if action == "block":
                exit_code = max(exit_code, 1)
            hits.append(GateHit("drifting", action, drifting,
                                 {c: _case_evidence(summary_view, c)
                                  for c in drifting}))

    return {
        "schema": SCHEMA_GATE,
        "experimentId": experiment.get("id"),
        "datasetDigest": experiment.get("datasetDigest"),
        "baselineId": comparison["baseline"]["experimentId"]
        if comparison is not None else None,
        "mode": comparison["mode"] if comparison is not None else "single",
        "policy": policy.to_dict(),
        "passed": exit_code == 0,
        "exitCode": exit_code,
        "hits": [h.to_dict() for h in hits],
    }


def render_gate_report(gate: dict) -> str:
    status = "放行" if gate["passed"] else f"阻断（exit {gate['exitCode']}）"
    lines = [
        "# Gate 报告",
        "",
        f"- experiment: {gate['experimentId']}"
        f"（dataset {gate['datasetDigest']}）",
        f"- baseline: {gate['baselineId'] or '（无）'}（mode: {gate['mode']}）",
        f"- 结论：**{status}**",
        f"- policy: {json.dumps(gate['policy'], ensure_ascii=False, sort_keys=True)}",
        "",
    ]
    if not gate["hits"]:
        lines.append("命中规则：无（全部通过或未命中任何规则）")
    for hit in gate["hits"]:
        lines.append(f"## 命中规则：`{hit['rule']}`（{hit['action']}）")
        for case_id, evidence in hit["evidence"].items():
            codes = ",".join(evidence.get("failureCodes") or []) or "-"
            lines.append(
                f"- {case_id}：verdict={evidence['verdict']}，"
                f"label={evidence['stabilityLabel']}，"
                f"failureCodes: {codes}，runs: {evidence['runStatuses']}")
            if "baseline" in evidence:
                lines.append(f"  - baseline 对比：{evidence['baseline']}")
        lines.append("")
    return "\n".join(lines) + "\n"


# ── CLI ──────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m trace_eval.gates")
    sub = parser.add_subparsers(dest="command", required=True)
    p_check = sub.add_parser("check", help="Gate 判定")
    p_check.add_argument("experiment")
    p_check.add_argument("--baseline", type=Path, default=None)
    p_check.add_argument("--policy", type=Path, default=None)
    p_check.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.command != "check":
        return 2
    try:
        policy = GatePolicy.load(args.policy) if args.policy else \
            GatePolicy.default()
        gate = evaluate_gate(Path(args.experiment),
                             baseline_dir=args.baseline, policy=policy)
    except (GateConfigError, AggregateError, ValueError,
            FileNotFoundError) as error:
        # 配置错误 / 判定不可信 → 2（不是产品失败的 1）
        print(f"error: {error}")
        return 2

    out = args.out or Path(args.experiment)
    out.mkdir(parents=True, exist_ok=True)
    (Path(out) / "gate.json").write_text(
        dumps_stable(gate) + "\n", encoding="utf-8")
    (Path(out) / "gate.report.md").write_text(
        render_gate_report(gate), encoding="utf-8")
    for hit in gate["hits"]:
        print(f"[{hit['action']}] {hit['rule']}: {hit['cases']}")
    print(f"gate: {'PASS' if gate['passed'] else 'BLOCKED'}"
          f"（exit {gate['exitCode']}）→ {Path(out) / 'gate.json'}")
    return gate["exitCode"]


if __name__ == "__main__":
    raise SystemExit(main())
