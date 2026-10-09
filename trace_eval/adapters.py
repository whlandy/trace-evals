#!/usr/bin/env python3
"""Round 3：Replay Adapter Protocol 与 Web / Maa / Desktop 实现。

协议（设计 §8.1）：

    preflight(case, variant)  -> PreflightResult   环境/输入可用性
    prepare(case, workspace)  -> PreparedRun       准备，不产生业务写
    run(prepared)             -> ExecutionArtifact 总是生成、或明确说明为何不能
    collect(prepared)         -> list[ArtifactRef] 收集产物
    cleanup(prepared, outcome)-> CleanupResult     成功/业务失败/异常/取消都执行

不变量（设计 §8.1）：
- `preflight` 和 `prepare` 不产生未声明的业务写操作；
- `run` 总是生成或明确说明为什么无法生成 Execution Artifact；
- `cleanup` 在成功、业务失败、异常和取消后都执行（runner 保证调用）；
- 清理失败单独记录，不能覆盖主要失败原因；
- **Adapter 不直接计算质量分数** —— 评分是 Evaluator 的职责（Round 1/4）。

本代码库的回放是**基于录制 Artifact 的离线回放**（与 trust/ 一致）：
Adapter 校验完整性、暂存并产出结构化回放证据；对真实目标系统的
写操作在环境接入后由 run() 承担，dry-run 下永远不发生。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from trace_eval.artifacts import file_digest
from trace_eval.contracts import ArtifactRef, EvalCase


@dataclass
class VariantSpec:
    """单次运行的变体声明（设计 §7.2 的最小实现；Round 3 单变体 v1）。"""
    id: str = "v1"
    notes: Optional[str] = None


@dataclass
class PreflightResult:
    ok: bool
    reason: Optional[str] = None
    blocked_kind: str = "env"  # env（环境不可用）| auth（认证失效 → invalid）
    side_effects: list = field(default_factory=list)


@dataclass
class PreparedRun:
    case_id: str
    adapter: str
    workspace: Path
    inputs: dict = field(default_factory=dict)  # 名称 → 快照内源路径


@dataclass
class ExecutionArtifact:
    """run 的产物：要么生成了执行证据，要么明确说明为什么不能。"""
    status: str  # ok | rejected
    notes: Optional[str] = None
    manifest: Optional[dict] = None  # 结构化回放证据（observed 事实，不是分数）


@dataclass
class CleanupResult:
    ok: bool
    errors: list = field(default_factory=list)


@runtime_checkable
class ReplayAdapter(Protocol):
    name: str

    def preflight(self, case: EvalCase, variant: VariantSpec) -> PreflightResult: ...
    def prepare(self, case: EvalCase, workspace: Path) -> PreparedRun: ...
    def run(self, prepared: PreparedRun) -> ExecutionArtifact: ...
    def collect(self, prepared: PreparedRun) -> list[ArtifactRef]: ...
    def cleanup(self, prepared: PreparedRun, outcome: ExecutionArtifact) -> CleanupResult: ...


def _stage(inputs: dict, workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    for name, source in inputs.items():
        source = Path(source)
        destination = workspace / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


class MaaReplayAdapter:
    """Maa 离线回放：校验录制 Execution 与快照 Golden 的完整性一致。"""
    name = "maa"

    def preflight(self, case: EvalCase, variant: VariantSpec) -> PreflightResult:
        for key in ("golden", "execution"):
            if not (case.input or {}).get(key):
                return PreflightResult(ok=False,
                                       reason=f"缺少 {key} Artifact（输入未声明）",
                                       blocked_kind="env")
        return PreflightResult(ok=True)

    def prepare(self, case: EvalCase, workspace: Path) -> PreparedRun:
        _stage({k: v for k, v in (case.input or {}).items()
                if k in ("golden", "execution")}, workspace)
        return PreparedRun(case_id=case.id, adapter=self.name,
                           workspace=workspace, inputs=dict(case.input or {}))

    def run(self, prepared: PreparedRun) -> ExecutionArtifact:
        from trust.maa_execution import trace_digest  # 摘要工具，不是评分
        golden_path = prepared.workspace / "golden.json"
        execution_path = prepared.workspace / "execution.json"
        try:
            golden = json.loads(golden_path.read_text(encoding="utf-8"))
            execution = json.loads(execution_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return ExecutionArtifact(status="rejected",
                                      notes=f"无法读取回放输入：{error}")
        if golden.get("$meta", {}).get("attach", {}).get("status") != "ready":
            return ExecutionArtifact(status="rejected",
                                      notes="Golden 不是 ready 状态（不完整）")
        recorded = execution.get("golden", {})
        actual = trace_digest(golden)
        if recorded.get("digest") != actual:
            return ExecutionArtifact(
                status="rejected",
                notes=f"Execution 与 Golden 不一致：记录 {recorded.get('digest')} "
                      f"≠ 实际 {actual}（拿错 Golden 的执行不可信）")
        return ExecutionArtifact(
            status="ok",
            manifest={"schema": "trace-evals.replay/v1",
                       "adapter": self.name,
                       "goldenDigest": actual,
                       "expectedNodeOrder": recorded.get("nodeOrder"),
                       "observedStepCount": len(execution.get("steps", []))})

    def collect(self, prepared: PreparedRun) -> list[ArtifactRef]:
        refs = []
        for name in ("golden.json", "execution.json"):
            path = prepared.workspace / name
            if path.exists():
                refs.append(ArtifactRef(path=str(Path(name)),
                                        digest=file_digest(path)))
        return refs

    def cleanup(self, prepared: PreparedRun,
                outcome: ExecutionArtifact) -> CleanupResult:
        return CleanupResult(ok=True)


class WebReplayAdapter:
    """Web 离线回放：Trace 与 Recording 的结构化对照证据（不评分）。"""
    name = "web"

    def preflight(self, case: EvalCase, variant: VariantSpec) -> PreflightResult:
        if not (case.input or {}).get("trace"):
            return PreflightResult(ok=False,
                                   reason="缺少 trace Artifact（输入未声明）",
                                   blocked_kind="env")
        if not (case.input or {}).get("recording"):
            return PreflightResult(ok=False,
                                   reason="缺少 recording Artifact（输入未声明）",
                                   blocked_kind="env")
        return PreflightResult(ok=True)

    def prepare(self, case: EvalCase, workspace: Path) -> PreparedRun:
        _stage({k: v for k, v in (case.input or {}).items()
                if k in ("trace", "recording")}, workspace)
        return PreparedRun(case_id=case.id, adapter=self.name,
                           workspace=workspace, inputs=dict(case.input or {}))

    def run(self, prepared: PreparedRun) -> ExecutionArtifact:
        try:
            trace = json.loads((prepared.workspace / "trace.json")
                                .read_text(encoding="utf-8"))
            recording = json.loads((prepared.workspace / "recording.json")
                                   .read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return ExecutionArtifact(status="rejected",
                                      notes=f"无法读取回放输入：{error}")
        node_order = trace.get("$meta", {}).get("attach", {}).get("nodeOrder") or []
        steps = recording.get("steps", [])
        manifest = {"schema": "trace-evals.replay/v1", "adapter": self.name,
                    "traceNodeCount": len(node_order),
                    "recordingStepCount": len(steps),
                    "nodeIds": [n.get("id") for n in
                                 trace.get("nodes", [])],
                    "recordingStepIds": [s.get("id") for s in steps],
                    "structurallyConsistent": (
                        sorted(nid for nid in
                               [n.get("id") for n in trace.get("nodes", [])]
                               if nid) ==
                        sorted(sid for sid in
                               [s.get("id") for s in steps] if sid))}
        return ExecutionArtifact(status="ok", manifest=manifest)

    def collect(self, prepared: PreparedRun) -> list[ArtifactRef]:
        refs = []
        for name in ("trace.json", "recording.json"):
            path = prepared.workspace / name
            if path.exists():
                refs.append(ArtifactRef(path=str(Path(name)),
                                        digest=file_digest(path)))
        return refs

    def cleanup(self, prepared: PreparedRun,
                outcome: ExecutionArtifact) -> CleanupResult:
        return CleanupResult(ok=True)


class DesktopReplayAdapter:
    """Desktop 接口桩：真实执行环境接入前，preflight 永远阻断（环境类，非业务失败）。"""
    name = "desktop"

    def preflight(self, case: EvalCase, variant: VariantSpec) -> PreflightResult:
        return PreflightResult(ok=False,
                               reason="desktop 执行环境未接入（preflight 桩）",
                               blocked_kind="env")

    def prepare(self, case: EvalCase, workspace: Path) -> PreparedRun:
        return PreparedRun(case_id=case.id, adapter=self.name,
                           workspace=workspace, inputs={})

    def run(self, prepared: PreparedRun) -> ExecutionArtifact:
        return ExecutionArtifact(status="rejected",
                                  notes="desktop 执行环境未接入")

    def collect(self, prepared: PreparedRun) -> list[ArtifactRef]:
        return []

    def cleanup(self, prepared: PreparedRun,
                outcome: ExecutionArtifact) -> CleanupResult:
        return CleanupResult(ok=True)


ADAPTERS = {
    "maa": MaaReplayAdapter(),
    "web": WebReplayAdapter(),
    "desktop": DesktopReplayAdapter(),
}


def adapter_for(case: EvalCase) -> ReplayAdapter:
    adapter = ADAPTERS.get(case.executor)
    if adapter is None:
        raise ValueError(f"未知 executor：{case.executor!r}（可选 {tuple(ADAPTERS)}）")
    return adapter
