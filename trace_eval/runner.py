#!/usr/bin/env python3
"""Round 3：Experiment Runner —— 单 Variant 实验的生命周期。

职责（设计 Round 3 代码修改清单）：

- 只接受**通过验证的 Dataset snapshot**（Round 2 的 Exit 条件在这里生效）；
- 每个 Case 走 adapter 生命周期 preflight → prepare → run → collect → cleanup；
- dry-run：只 preflight + 输出计划，不 prepare、不 run、不写任何 Run；
- 超时（线程 join timeout）、Ctrl-C（KeyboardInterrupt）、执行异常、
  业务失败后 cleanup 都执行；清理失败单独记录，不覆盖主失败原因；
- 记录 Git SHA、Dataset digest、Variant、Trial、Artifact 摘要进 EvalRun；
- preflight 阻断映射：auth 失效 → `invalid`（不计业务失败）；
  环境不可用（含 Desktop 桩）→ `infra_error`。

退出条件（设计）：一条命令产生可复查的单 Variant Experiment：

    python3 -m trace_eval.runner run <snapshot-dir> [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from trace_eval.adapters import (
    ReplayAdapter, VariantSpec, adapter_for,
)
from trace_eval.artifacts import dataset_digest
from trace_eval.contracts import ArtifactRef, EvalRun
from trace_eval.datasets import DatasetError, load_dataset, validate_dataset
from trace_eval.storage import ExperimentStore

SCHEMA_EXPERIMENT_CONFIG = "trace-evals.experiment-config/v1"


class RunnerError(RuntimeError):
    """配置/环境层面的错误（区别于 Case 的业务失败）。"""


@dataclass
class RunConfig:
    snapshot_dir: Path
    experiment_id: str
    store_root: Path
    variant_id: str = "v1"
    dry_run: bool = False
    timeout: float = 30.0


@dataclass
class PlannedRun:
    case_id: str
    executor: str
    preflight_ok: bool
    preflight_reason: str | None = None
    would_write: list = field(default_factory=list)  # 离线回放：恒为空


@dataclass
class ExperimentSummary:
    experiment_id: str
    experiment_dir: Path | None = None
    dry_run_plan: list | None = None
    run_dirs: list = field(default_factory=list)
    statuses: dict = field(default_factory=dict)  # runId -> status


def repo_git_sha(repo_root: Path | None = None) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root or Path(__file__).resolve().parents[1]),
             "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=10).stdout.strip()
        return out or None
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _load_validated_snapshot(snapshot_dir: Path):
    dataset = load_dataset(snapshot_dir)
    problems = validate_dataset(dataset)
    if problems:
        raise RunnerError(
            "Experiment 只接受通过验证的 Dataset snapshot："
            + "；".join(problems))
    return dataset


def _execute_adapter(adapter: ReplayAdapter, prepared,
                     timeout: float) -> tuple[str, str | None, object]:
    """在线程里跑 run()，主线程按 timeout / KeyboardInterrupt / 异常分流。

    返回 (kind, note, payload)：kind ∈ done | timeout | error | cancelled
    """
    box: dict = {}

    def worker() -> None:
        try:
            box["outcome"] = adapter.run(prepared)
        except BaseException as error:  # noqa: BLE001 —— 分类由主线程做
            box["error"] = error

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        thread.join(timeout)
    except KeyboardInterrupt:
        thread.join(1.0)
        return "cancelled", "Ctrl-C 取消（KeyboardInterrupt）", None
    if thread.is_alive():
        return "timeout", f"run 超过 {timeout}s 未完成", None
    if "error" in box:
        error = box["error"]
        if isinstance(error, KeyboardInterrupt):
            return "cancelled", "Ctrl-C 取消（KeyboardInterrupt）", None
        return "error", f"{type(error).__name__}: {error}", None
    return "done", None, box.get("outcome")


def _materialize_artifacts(run_dir: Path, prepared,
                           refs: list) -> list:
    """把 adapter 收集到的 Artifact 复制进 run_dir/artifacts/（Run 自包含、
    从 Run ID 即可定位全部 Artifact），并让 ArtifactRef.path 指向真实位置。"""
    out = []
    for ref in refs:
        name = Path(ref.path).name
        source = prepared.workspace / name
        if not source.exists():
            continue
        target_dir = run_dir / "artifacts"
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target_dir / name)
        out.append(replace(ref, path=f"artifacts/{name}"))
    return out


def run_case(config: RunConfig, case, adapter: ReplayAdapter,
             handle, store: ExperimentStore,
             dataset_digest_value: str) -> Path:
    variant = VariantSpec(id=config.variant_id)
    run_id = store.new_run_id(case.id, config.variant_id, trial=1)
    run_dir = store.run_dir(handle, run_id)
    workspace = run_dir / "workspace"

    # Case 输入的 Artifact 路径是快照内相对路径 —— 进 adapter 前解析为绝对路径
    # （adapter 只见 workspace 与绝对源路径，不依赖 CWD）
    case_abs = replace(case, input={
        k: str(Path(config.snapshot_dir) / v)
        for k, v in (case.input or {}).items()})

    status: str
    note: str | None = None
    artifact_refs: list[ArtifactRef] = []
    prepared = None
    outcome = None
    cleanup_record: dict = {"ok": True, "errors": []}

    pre = adapter.preflight(case_abs, variant)
    if not pre.ok:
        # 环境/认证阻断：不执行、不准备；记录原因，绝不算业务失败
        status = "invalid" if pre.blocked_kind == "auth" else "infra_error"
        note = pre.reason
    else:
        prepared = adapter.prepare(case_abs, workspace)
        kind, kind_note, outcome = _execute_adapter(
            adapter, prepared, config.timeout)
        if kind == "cancelled":
            status, note = "cancelled", kind_note
        elif kind == "timeout":
            status, note = "infra_error", f"超时（{kind_note}）"
        elif kind == "error":
            status, note = "infra_error", kind_note
        else:
            if outcome is None:
                status, note = "infra_error", "adapter.run 未返回产物"
            elif outcome.status == "ok":
                status, note = "completed", outcome.notes
                artifact_refs = _materialize_artifacts(
                    run_dir, prepared, adapter.collect(prepared))
            else:
                # 完整性被拒（如 Execution 与 Golden 不一致）：不是业务失败
                status, note = "invalid", outcome.notes

    # cleanup：成功、业务失败、异常、取消后都执行；失败单独记录
    if prepared is not None:
        try:
            result = adapter.cleanup(prepared, outcome)
            cleanup_record = {"ok": result.ok, "errors": list(result.errors)}
        except Exception as error:  # noqa: BLE001 —— 清理失败不覆盖主原因
            cleanup_record = {"ok": False,
                              "errors": [f"{type(error).__name__}: {error}"]}

    environment = {
        "gitSha": repo_git_sha(),
        "datasetDigest": dataset_digest_value,
        "variantId": config.variant_id,
        "trial": 1,
        "adapter": adapter.name,
        "timeout": config.timeout,
        "startedAt": _ts(),
    }
    run = EvalRun(
        run_id=run_id,
        experiment_id=config.experiment_id,
        case_id=case.id,
        variant_id=config.variant_id,
        trial=1,
        status=status,
        started_at=environment["startedAt"],
        finished_at=_ts(),
        artifacts={ref.path: {"path": ref.path, "digest": ref.digest}
                   for ref in artifact_refs},
        environment=environment,
        extra={"note": note, "cleanup": cleanup_record},
    )
    store.commit_run(run_dir, run, results=[])
    return run_dir


def run_experiment(config: RunConfig) -> ExperimentSummary:
    config.snapshot_dir = Path(config.snapshot_dir)
    config.store_root = Path(config.store_root)
    dataset = _load_validated_snapshot(config.snapshot_dir)
    digest_value = dataset_digest(config.snapshot_dir)

    if config.dry_run:
        # 只 preflight：不 begin、不 prepare、不 run、不写任何文件
        # —— dry-run 零写操作（连本地 store 都不创建）
        plan = []
        for case in dataset.cases:
            adapter = adapter_for(case)
            pre = adapter.preflight(case, VariantSpec(id=config.variant_id))
            plan.append(PlannedRun(case.id, case.executor, pre.ok, pre.reason))
        return ExperimentSummary(config.experiment_id, dry_run_plan=plan)

    store = ExperimentStore(config.store_root)
    handle = store.begin(config.experiment_id, digest_value, {
        "schema": SCHEMA_EXPERIMENT_CONFIG,
        "variantId": config.variant_id,
        "timeout": config.timeout,
        "snapshotId": dataset.id,
    })
    summary = ExperimentSummary(config.experiment_id, handle.experiment_dir)
    for case in dataset.cases:
        run_dir = run_case(config, case, adapter_for(case), handle, store,
                           digest_value)
        summary.run_dirs.append(run_dir)
        summary.statuses[run_dir.name] = _status_of(run_dir, store)
    return summary


def _status_of(run_dir: Path, store: ExperimentStore) -> str:
    try:
        return store.load_run(run_dir).status
    except Exception:  # noqa: BLE001 —— 定稿失败本身是事故，报给上层
        raise RunnerError(f"Run 定稿后无法解析：{run_dir}") from None


def _ts() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m trace_eval.runner")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="执行单 Variant Experiment")
    p_run.add_argument("snapshot")
    p_run.add_argument("--experiment-id", default=None)
    p_run.add_argument("--store", type=Path, default=None,
                      help="Run 存储根目录（默认 <snapshot>/.experiments）")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    if args.command != "run":
        return 2
    snapshot = Path(args.snapshot)
    try:
        dataset = load_dataset(snapshot)
        config = RunConfig(
            snapshot_dir=snapshot,
            experiment_id=args.experiment_id or f"exp-{int(time.time())}",
            store_root=args.store or snapshot / ".experiments",
            dry_run=args.dry_run,
            timeout=args.timeout,
        )
        summary = run_experiment(config)
    except (RunnerError, DatasetError) as error:
        print("error:", error)
        return 2

    if summary.dry_run_plan is not None:
        for plan in summary.dry_run_plan:
            mark = "ok " if plan.preflight_ok else "blk"
            print(f"[{mark}] {plan.case_id} ({plan.executor})"
                  + (f"：{plan.preflight_reason}" if plan.preflight_reason else ""))
        print("dry-run：未产生任何写操作")
        return 0
    for run_dir, status in zip(summary.run_dirs, summary.statuses.values()):
        print(f"{status:<13} {run_dir}")
    print(f"experiment: {summary.experiment_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
