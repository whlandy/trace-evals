#!/usr/bin/env python3
"""Round 2：摘要寻址的 Artifact（设计 §5.3）。

进入 Experiment 的 Golden、Recording、Execution、截图和报告以摘要寻址，
同一个 digest 不得对应不同内容。

本模块只负责「字节 → 摘要」与 manifest 的读写：

- `file_digest`：文件字节的 sha256（`sha256:<hex>`，与 trace_digest 同族前缀）；
- `manifest.jsonl`：每行一条 `{"path": 相对路径, "digest": ..., "size": ...}`，
  按 path 排序、字符级稳定序列化（trust.stable_json）——同内容同字节；
- `dataset_digest`：dataset.json + manifest 的规范化联合摘要。
  任一 Artifact 被修改（manifest 重算后）或 dataset.json 变化，digest 必变。

Dataset 自身的加载、校验与快照在 datasets.py。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from trust.stable_json import dumps_stable
from trace_eval.contracts import ContractError

MANIFEST_NAME = "manifest.jsonl"


def file_digest(path: Path) -> str:
    """文件字节的 sha256。分块读，大 Artifact 不吃内存。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def write_manifest(dataset_dir: Path, entries: list[dict]) -> None:
    """把 manifest 条目按 path 排序后落盘（每行一条稳定 JSON）。"""
    ordered = sorted(entries, key=lambda entry: entry["path"])
    text = "\n".join(dumps_stable(entry) for entry in ordered)
    (dataset_dir / MANIFEST_NAME).write_text(text + "\n", encoding="utf-8")


def read_manifest(dataset_dir: Path) -> list[dict]:
    path = dataset_dir / MANIFEST_NAME
    if not path.exists():
        return []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    entries = []
    for line_number, line in enumerate(lines, 1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            raise ContractError(f"manifest 第 {line_number} 行不是合法 JSON：{error}") from error
        if not isinstance(entry, dict) or "path" not in entry or "digest" not in entry:
            raise ContractError(f"manifest 第 {line_number} 行缺少 path/digest")
        entries.append(entry)
    return entries


def dataset_digest(dataset_dir: Path) -> str:
    """dataset.json + manifest 的联合摘要。同内容同 digest，任一变化 digest 变。"""
    payload = {
        "dataset": json.loads((dataset_dir / "dataset.json").read_text(encoding="utf-8")),
        "manifest": read_manifest(dataset_dir),
    }
    canonical = dumps_stable(payload).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()
