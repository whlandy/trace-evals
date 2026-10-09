#!/usr/bin/env python3
"""Round 4：C1–C4 分层报告与 trace_correct 合取判定。

设计 §4：「Trace 正确」必须拆成四层，报告中不得混写：

    trace_correct =
        structure_valid            (C1)
        AND execution_conformant   (C2)
        AND business_oracles_passed (C3)
        AND golden_accepted        (C4)
        AND stability_gate_passed  (C5，Round 5 接入)

判定规则（设计 §4 + §5.1 证据优先）：

- 任一层 fail → 整体 **fail**（fail 优先于 inconclusive）；
- 某一层缺少证据 → 该层 **inconclusive**，整体不得为 pass；
- 无 fail 且四层全 pass 但稳定性门证据缺失（Round 4 阶段）→ **inconclusive**
  —— 可复现性未建立之前，不允许全绿；
- 每层结论携带自己的证据，报告逐层展开，**不折叠成单一总分**。

层与结果键前缀的绑定（Round 1 统一协议）：

    C1 ← replay.structure.*   本模块从 Run Artifact 重放推导（只读、确定性）
    C2 ← execution.*          Round 1 MaaExecutionEvaluator（含 integrity 硬失败）
    C3 ← oracle.*             Round 4 Oracle（业务结果）
    C4 ← golden.trust.*       Round 1 GoldenTrustEvaluator（Golden 可信度）
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from trace_eval.contracts import EvaluatorResult, parse_eval_run

C1_PREFIX = "replay.structure."
C2_PREFIX = "execution."
C3_PREFIX = "oracle."
C4_PREFIX = "golden.trust."
C5_PREFIX = "stability."

LAYER_NAMES = {
    "C1": ("structure_valid", C1_PREFIX),
    "C2": ("execution_conformant", C2_PREFIX),
    "C3": ("business_oracles_passed", C3_PREFIX),
    "C4": ("golden_accepted", C4_PREFIX),
}

STRUCTURE_EVALUATOR = {"name": "replay-structure", "version": "1.0.0"}


# ── C1：从 Run Artifact 重放推导结构证据（只读、确定性）────────────


def structure_results(run_dir: Path) -> list[EvaluatorResult]:
    """对 Run 目录内的 Artifact 做结构一致性检查（不评分、不依赖 Adapter）。

    - maa：Golden ready + 节点序一致 + Execution 声称的 Golden 摘要与实际一致；
    - web：Trace 节点集与 Recording 步骤集一致。
    """
    from trust.maa_execution import trace_digest
    run_dir = Path(run_dir)
    artifacts = run_dir / "artifacts"
    try:
        run = parse_eval_run(json.loads(
            (run_dir / "run.json").read_text(encoding="utf-8")))
        case_id = run.case_id
    except Exception:  # noqa: BLE001 —— 无定稿 run.json 时退化用目录名
        case_id = run_dir.name

    results: list[EvaluatorResult] = []

    golden = artifacts / "golden.json"
    execution = artifacts / "execution.json"
    if golden.is_file() and execution.is_file():
        g = json.loads(golden.read_text(encoding="utf-8"))
        e = json.loads(execution.read_text(encoding="utf-8"))
        findings = []
        if g.get("$meta", {}).get("attach", {}).get("status") != "ready":
            findings.append("Golden 不是 ready 状态")
        g_order = g.get("$meta", {}).get("attach", {}).get("nodeOrder") or []
        e_order = (e.get("golden") or {}).get("nodeOrder") or []
        if g_order != e_order:
            findings.append(f"节点序不一致：Golden={g_order} Execution={e_order}")
        if (e.get("golden") or {}).get("digest") != trace_digest(g):
            findings.append("Execution 声称的 Golden 摘要与实际不符")
        if findings:
            results.append(EvaluatorResult(
                key=f"{C1_PREFIX}maa.{case_id}", scope="run", verdict="fail",
                calibration="deterministic",
                failure_code="STRUCTURE_GOLDEN_EXECUTION_MISMATCH",
                evidence=findings,
                evaluator=dict(STRUCTURE_EVALUATOR)))
        else:
            results.append(EvaluatorResult(
                key=f"{C1_PREFIX}maa.{case_id}", scope="run", verdict="pass",
                calibration="deterministic",
                observed={"nodeOrder": g_order,
                          "goldenDigest": (e.get("golden") or {}).get("digest")},
                evaluator=dict(STRUCTURE_EVALUATOR)))
        return results

    trace = artifacts / "trace.json"
    recording = artifacts / "recording.json"
    if trace.is_file() and recording.is_file():
        t = json.loads(trace.read_text(encoding="utf-8"))
        r = json.loads(recording.read_text(encoding="utf-8"))
        node_ids = sorted(n.get("id") for n in t.get("nodes", []) if n.get("id"))
        step_ids = sorted(s.get("id") for s in r.get("steps", []) if s.get("id"))
        if node_ids == step_ids:
            results.append(EvaluatorResult(
                key=f"{C1_PREFIX}web.{case_id}", scope="run", verdict="pass",
                calibration="deterministic",
                observed={"nodeIds": node_ids, "stepIds": step_ids},
                evaluator=dict(STRUCTURE_EVALUATOR)))
        else:
            results.append(EvaluatorResult(
                key=f"{C1_PREFIX}web.{case_id}", scope="run", verdict="fail",
                calibration="deterministic",
                failure_code="STRUCTURE_NODE_STEP_MISMATCH",
                expected={"nodeIds": node_ids},
                observed={"stepIds": step_ids},
                evidence=[f"Trace 节点与 Recording 步骤不一致：{node_ids} vs {step_ids}"],
                evaluator=dict(STRUCTURE_EVALUATOR)))
        return results

    # 两种回放形态都不具备 → 结构层没有证据
    results.append(EvaluatorResult(
        key=f"{C1_PREFIX}none.{case_id}", scope="run", verdict="inconclusive",
        calibration="deterministic",
        comment="Run 内没有可推导结构的 Artifact 组合（golden+execution / trace+recording）",
        evaluator=dict(STRUCTURE_EVALUATOR)))
    return results


# ── 分层判定 ──────────────────────────────────────────────────────


@dataclasses.dataclass
class LayerVerdict:
    layer: str            # C1..C5
    label: str            # structure_valid 等
    verdict: str          # pass | fail | inconclusive
    evidence: list = dataclasses.field(default_factory=list)
    detail: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class TraceJudgment:
    trace_correct: str    # pass | fail | inconclusive
    layers: dict          # "C1" -> LayerVerdict
    note: str | None = None

    def to_dict(self) -> dict:
        return {"trace_correct": self.trace_correct,
                "layers": {k: v.to_dict() for k, v in self.layers.items()},
                "note": self.note}


def _layer_from_results(results: list, prefix: str, layer: str,
                        label: str) -> LayerVerdict:
    scoped = [r for r in results if r.key.startswith(prefix)]
    evidence = [f"{r.key}: {r.verdict}" for r in scoped]
    if not scoped:
        return LayerVerdict(layer, label, "inconclusive",
                            detail="该层没有证据（缺失不得自动按通过处理）")
    if any(r.verdict == "fail" for r in scoped):
        failed = [r for r in scoped if r.verdict == "fail"]
        detail = "；".join(
            f"{r.key}：{r.failure_code or 'fail'}"
            + (f"（{r.comment}）" if r.comment else "")
            for r in failed)
        return LayerVerdict(layer, label, "fail", evidence, detail)
    if any(r.verdict == "inconclusive" for r in scoped):
        pending = [r.key for r in scoped if r.verdict == "inconclusive"]
        return LayerVerdict(layer, label, "inconclusive", evidence,
                            f"证据缺失：{pending}")
    return LayerVerdict(layer, label, "pass", evidence)


def judge(results: list, stability: LayerVerdict | None = None) -> TraceJudgment:
    """C1–C4（+可选 C5 稳定性门）合取判定。"""
    layers: dict[str, LayerVerdict] = {}
    for layer, (label, prefix) in LAYER_NAMES.items():
        layers[layer] = _layer_from_results(results, prefix, layer, label)
    if stability is not None:
        layers["C5"] = stability

    verdicts = [v.verdict for v in layers.values()]
    if "fail" in verdicts:
        overall, note = "fail", "任一层失败 → 整体失败（fail 优先于 inconclusive）"
    elif "inconclusive" in verdicts:
        pending = [k for k, v in layers.items() if v.verdict == "inconclusive"]
        overall, note = "inconclusive", f"证据不足层：{pending}"
    elif stability is None:
        overall, note = "inconclusive", \
            "稳定性门（C5）证据缺失：可复现性未建立前不允许全绿（Round 5 接入）"
    else:
        overall, note = "pass", "四层 + 稳定性门全过"
    return TraceJudgment(overall, layers, note)


def render_report(judgment: TraceJudgment) -> str:
    """逐层展开的分层报告 —— 不折叠成单一总分（设计 §4）。"""
    lines = [f"trace_correct = {judgment.trace_correct.upper()}"]
    if judgment.note:
        lines.append(f"    （{judgment.note}）")
    for layer in ("C1", "C2", "C3", "C4", "C5"):
        verdict = judgment.layers.get(layer)
        if verdict is None:
            lines.append(f"├─ {layer} — 未提供")
            continue
        mark = {"pass": "✓", "fail": "✗", "inconclusive": "?"}[verdict.verdict]
        line = f"├─ {layer} {verdict.label:<26} {verdict.verdict} {mark}"
        if verdict.detail:
            line += f"  ← {verdict.detail}"
        lines.append(line)
        for item in verdict.evidence:
            lines.append(f"    · {item}")
    return "\n".join(lines)
