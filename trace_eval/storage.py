#!/usr/bin/env python3
"""Round 3：Run Artifact 存储（原子落盘、从 Run ID 定位全部 Artifact）。

布局：

    <store_root>/<experimentId>/
    ├── experiment.json          实验登记（dataset digest、config、时间）
    └── runs/<runId>/
        ├── run.json             已定稿的 EvalRun（仅通过原子 rename 出现）
        ├── run.json.tmp         落盘中（进程在此中断 = 半份 Run）
        ├── results.jsonl        EvaluatorResult（有结果时才存在）
        └── workspace/...        adapter 暂存/产物

原子性规则（验收：进程在落盘中途终止不会产生被解析为 completed 的半份 Run）：

- 一切内容先写 `.tmp`，全部就位后 `os.replace` 成 `run.json`；
- `load_run` 只认 `run.json`；没有它 = 这个 Run 不存在（StorageError），
  半份 Run 永远不可能被解析成 completed；
- Run ID 形如 `<caseId>__<variantId>__t<trial>__<8位时间戳>`，
  同一 Experiment 内唯一，且 run 目录名本身就是全部 Artifact 的定位路径。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from trace_eval.contracts import EvaluatorResult, EvalRun, parse_eval_run
from trust.stable_json import dumps_stable

RUN_JSON = "run.json"
RUN_TMP = "run.json.tmp"
RESULTS_JSONL = "results.jsonl"
EXPERIMENT_JSON = "experiment.json"


class StorageError(RuntimeError):
    """Run 不存在或不可解析（半份 Run 一律按「不存在」处理）。"""


@dataclass
class ExperimentHandle:
    experiment_id: str
    experiment_dir: Path

    @property
    def runs_dir(self) -> Path:
        return self.experiment_dir / "runs"


class ExperimentStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def begin(self, experiment_id: str, dataset_digest: str,
              config: dict) -> ExperimentHandle:
        experiment_dir = self.root / experiment_id
        experiment_dir.mkdir(parents=True, exist_ok=True)
        (experiment_dir / "runs").mkdir(exist_ok=True)
        _write_atomic(experiment_dir / EXPERIMENT_JSON, {
            "schema": "trace-evals.experiment/v1",
            "id": experiment_id,
            "datasetDigest": dataset_digest,
            "config": config,
            "startedAt": _now(),
        })
        return ExperimentHandle(experiment_id=experiment_id,
                                 experiment_dir=experiment_dir)

    def new_run_id(self, case_id: str, variant_id: str, trial: int) -> str:
        safe_case = case_id.replace("/", "-").replace(" ", "-")
        safe_variant = variant_id.replace("/", "-").replace(" ", "-")
        return f"{safe_case}__{safe_variant}__t{trial:02d}__{time.time_ns():08x}"

    def run_dir(self, handle: ExperimentHandle, run_id: str) -> Path:
        directory = handle.runs_dir / run_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def commit_run(self, run_dir: Path, run: EvalRun,
                   results: list[EvaluatorResult]) -> None:
        """原子定稿：先 .tmp，全部就位后 rename 出 run.json。"""
        run_dir = Path(run_dir)
        payload = dumps_stable(run.to_dict())
        tmp = run_dir / RUN_TMP
        final = run_dir / RUN_JSON
        tmp.write_text(payload + "\n", encoding="utf-8")
        if results:
            results_tmp = run_dir / (RESULTS_JSONL + ".tmp")
            results_tmp.write_text(
                "\n".join(dumps_stable(r.to_dict()) for r in results) + "\n",
                encoding="utf-8")
            results_tmp.replace(run_dir / RESULTS_JSONL)
        tmp.replace(final)  # 唯一「定稿」时刻

    def load_run(self, run_dir: Path) -> EvalRun:
        run_dir = Path(run_dir)
        final = run_dir / RUN_JSON
        if not final.exists():
            raise StorageError(
                f"{run_dir}: 没有定稿的 {RUN_JSON}（半份 Run 不可解析为任何状态）")
        try:
            return parse_eval_run(json.loads(final.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError) as error:
            raise StorageError(f"{run_dir}: run.json 损坏：{error}") from error

    def load_results(self, run_dir: Path) -> list[EvaluatorResult]:
        path = Path(run_dir) / RESULTS_JSONL
        if not path.exists():
            return []
        from trace_eval.contracts import parse_evaluator_result
        return [parse_evaluator_result(json.loads(line))
                for line in path.read_text(encoding="utf-8").splitlines() if line]

    def list_runs(self, handle: ExperimentHandle) -> list[Path]:
        if not handle.runs_dir.exists():
            return []
        return sorted(p for p in handle.runs_dir.iterdir() if p.is_dir())


def _write_atomic(path: Path, value: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(dumps_stable(value) + "\n", encoding="utf-8")
    tmp.replace(path)


def _now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
