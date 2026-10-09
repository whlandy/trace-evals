"""Phoenix 重复实验门禁：固定 dataset/rubric/prompt/model provenance。"""

from __future__ import annotations

from typing import Any, Callable

from trust.judge import RUBRIC_VERSION


def run_versioned_experiment(*, dataset: Any, task: Callable, evaluators: Any,
                             model: str, prompt_hash: str, dataset_version_id: str,
                             experiment_name: str, repetitions: int = 3,
                             dry_run: bool | int = False, phoenix_client=None,
                             rubric_version: str = RUBRIC_VERSION,
                             extra_metadata: dict | None = None):
    """正式实验至少两次；dataset version 不匹配时拒绝退回 latest。"""
    for name, value in (("model", model), ("prompt_hash", prompt_hash),
                        ("dataset_version_id", dataset_version_id),
                        ("experiment_name", experiment_name),
                        ("rubric_version", rubric_version)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 必须是非空字符串")
    if isinstance(repetitions, bool) or not isinstance(repetitions, int) or repetitions < 1:
        raise ValueError("repetitions 必须是正整数")
    if not dry_run and repetitions < 2:
        raise ValueError("正式 Phoenix experiment 至少 repetitions=2；单次仅允许 dry_run")
    actual_version = getattr(dataset, "version_id", None)
    if actual_version != dataset_version_id:
        raise ValueError(f"dataset version 不匹配：期望 {dataset_version_id}，实际 {actual_version}")
    if phoenix_client is None:
        try:
            from phoenix.client import Client
        except ImportError as error:
            raise RuntimeError("缺少 Phoenix client；执行 pip install -r requirements-phoenix.txt") from error
        phoenix_client = Client()
    metadata = {"traceEvalRubricVersion": rubric_version,
                "traceEvalPromptHash": prompt_hash,
                "traceEvalModel": model,
                "traceEvalDatasetVersionId": dataset_version_id,
                "traceEvalRepetitions": repetitions,
                "interpretation": "repeated experiment metrics; not calibrated probability"}
    overlap = set(metadata) & set(extra_metadata or {})
    if overlap:
        raise ValueError(f"extra_metadata 不得覆盖 provenance：{sorted(overlap)}")
    metadata.update(extra_metadata or {})
    return phoenix_client.experiments.run_experiment(
        dataset=dataset, task=task, evaluators=evaluators,
        experiment_name=experiment_name,
        experiment_description="Version-pinned repeated trace-eval experiment",
        experiment_metadata=metadata, repetitions=repetitions, dry_run=dry_run)
