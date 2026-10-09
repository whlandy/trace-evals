#!/usr/bin/env python3
"""Round 2：版本化 Dataset —— 加载、校验、登记与快照。

目标（设计文档 Round 2）：把零散目录变成可复现、可寻址的数据集。

一个 Dataset 目录：

    dataset/
    ├── dataset.json      Case 声明（EvalCase[]，schema trace-evals.dataset/v1）
    ├── manifest.jsonl    Artifact 登记（path → digest/size，摘要寻址）
    └── <case artifacts>   相对路径引用的文件（Golden / Recording / …）

校验清单（validate_dataset，全部命中才允许 snapshot）：

1. Case ID 唯一；
2. executor ∈ {web, desktop, maa}；
3. Artifact 相对路径、不逃逸 Dataset 目录、文件存在、
   manifest 已登记且 digest 与实际字节一致（§5.3 摘要寻址）；
4. 有写操作的 Case 必须声明 statePolicy（§5.5 副作用默认串行）；
5. dataset.json 与被引用的文本 Artifact 不含 Secret 形态。

CLI：

    python3 -m trace_eval.datasets validate <dataset-dir>
    python3 -m trace_eval.datasets snapshot <dataset-dir> [--to <snap-root>]

snapshot 是**独立副本**：快照之后修改源目录不影响已创建 Experiment 的输入
（Exit 条件：Experiment 只接受通过验证的 Dataset snapshot）。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import shutil
from pathlib import Path
from typing import Any, Optional

from trace_eval import artifacts
from trace_eval.contracts import ContractError, EvalCase, parse_eval_case

SCHEMA_DATASET = "trace-evals.dataset/v1"
DATASET_NAME = "dataset.json"
EXECUTORS = ("web", "desktop", "maa")

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)cookie\s*[:=]"),
    re.compile(r"(?i)(password|passwd|token)\s*[:=]\s*['\"][^'\"]{6,}"),
]


class DatasetError(ContractError):
    """Dataset 结构/完整性违例（可被 ContractError 捕获）。"""


@dataclasses.dataclass
class Dataset:
    id: str
    version: Optional[str]
    cases: list
    metadata: Optional[dict] = None
    extra: Optional[dict] = None
    root: Optional[Path] = dataclasses.field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict:
        out = {"schema": SCHEMA_DATASET, "id": self.id}
        if self.version is not None:
            out["version"] = self.version
        out["cases"] = [case.to_dict() for case in self.cases]
        if self.metadata is not None:
            out["metadata"] = self.metadata
        if self.extra:
            out.update(self.extra)
        return out

    @classmethod
    def from_dict(cls, value: dict) -> "Dataset":
        if not isinstance(value, dict):
            raise DatasetError("Dataset 必须是 JSON 对象")
        schema = value.get("schema")
        if schema != SCHEMA_DATASET:
            family = SCHEMA_DATASET.rsplit("/", 1)[0]
            if isinstance(schema, str) and schema.rsplit("/", 1)[0] == family:
                raise DatasetError(f"Dataset: 未知主版本 {schema!r}（期望 {SCHEMA_DATASET!r}）")
            raise DatasetError(f"Dataset: 缺少或无效 schema {schema!r}")
        dataset_id = value.get("id")
        if not isinstance(dataset_id, str) or not dataset_id:
            raise DatasetError("Dataset.id 必须是非空字符串")
        raw_cases = value.get("cases")
        if not isinstance(raw_cases, list) or not raw_cases:
            raise DatasetError("Dataset.cases 必须是非空数组")
        cases = [parse_eval_case(item, require_schema=False) for item in raw_cases]
        metadata = value.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise DatasetError("Dataset.metadata 必须是对象")
        known = {"schema", "id", "version", "cases", "metadata"}
        extra = {k: v for k, v in value.items() if k not in known}
        return cls(id=dataset_id, version=value.get("version"), cases=cases,
                   metadata=metadata, extra=extra or None)


def load_dataset(dataset_dir: Path) -> Dataset:
    dataset_dir = Path(dataset_dir)
    path = dataset_dir / DATASET_NAME
    if not path.exists():
        raise DatasetError(f"找不到 {path}")
    dataset = Dataset.from_dict(json.loads(path.read_text(encoding="utf-8")))
    dataset.root = dataset_dir
    return dataset


def _resolve(dataset_dir: Path, rel: str) -> Path:
    """把相对 Artifact 路径解析进 Dataset 目录；逃逸即拒绝。"""
    if rel.startswith("/") or rel.startswith("~"):
        raise DatasetError(f"Artifact 路径必须是相对路径：{rel}")
    root = dataset_dir.resolve()
    target = (dataset_dir / rel).resolve()
    if not target.is_relative_to(root):
        raise DatasetError(f"Artifact 路径逃逸 Dataset 目录：{rel}")
    return target


# Case 输入中可引用的 Artifact 键（Round 3 起：回放需要 execution / trace；
# Round 4 起：Oracle 证据走 evidence 键）
INPUT_KEYS = ("recording", "golden", "execution", "trace", "evidence")


def referenced_artifacts(cases: list) -> list[tuple[str, str]]:
    """(case_id, 相对路径) 对，按 Case 的 input.recording / golden / execution / trace。"""
    refs = []
    for case in cases:
        for key in INPUT_KEYS:
            rel = (case.input or {}).get(key)
            if rel:
                refs.append((case.id, rel))
    return refs


def build_manifest(dataset_dir: Path) -> list[dict]:
    """把 Dataset 引用的全部 Artifact 登记进 manifest（摘要寻址，§5.3）。"""
    dataset = load_dataset(dataset_dir)
    entries = []
    for case_id, rel in referenced_artifacts(dataset.cases):
        path = _resolve(dataset_dir, rel)
        if not path.exists():
            raise DatasetError(f"{case_id}: Artifact 缺失：{rel}")
        entries.append({"path": rel, "digest": artifacts.file_digest(path),
                        "size": path.stat().st_size})
    artifacts.write_manifest(dataset_dir, entries)
    return entries


def _side_effect_kind(effect: Any) -> str:
    return str(effect).split(":", 1)[0]


def validate_dataset(dataset: Dataset) -> list[str]:
    """返回问题列表；空列表 = 通过。"""
    problems: list[str] = []
    if dataset.root is None:
        raise DatasetError("validate 需要 load_dataset 提供的 root")
    root = dataset.root

    seen: set[str] = set()
    for case in dataset.cases:
        if case.id in seen:
            problems.append(f"重复 Case ID：{case.id}")
        seen.add(case.id)
        if case.executor not in EXECUTORS:
            problems.append(
                f"{case.id}: executor 必须是 {EXECUTORS} 之一，实际 {case.executor!r}")

    manifest = {entry["path"]: entry
                for entry in artifacts.read_manifest(root)}
    for case in dataset.cases:
        env = case.environment or {}
        side_effects = env.get("sideEffects") or []
        writes = [effect for effect in side_effects
                  if _side_effect_kind(effect) == "write"]
        if writes and not env.get("statePolicy"):
            problems.append(f"{case.id}: 有写操作 {writes} 但未声明 statePolicy")
        for case_id, rel in referenced_artifacts([case]):
            try:
                path = _resolve(root, rel)
            except DatasetError as error:
                problems.append(f"{case_id}: {error}")
                continue
            if not path.exists():
                problems.append(f"{case_id}: Artifact 缺失：{rel}")
                continue
            entry = manifest.get(rel)
            if entry is None:
                problems.append(f"{case_id}: Artifact 未登记 manifest：{rel}")
            elif entry["digest"] != artifacts.file_digest(path):
                problems.append(f"{case_id}: 摘要不符（Artifact 被改或 manifest 过期）：{rel}")

    for path, label in _scannable_texts(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                problems.append(f"{label} 命中 Secret 形态：{pattern.pattern}")
    return problems


def _scannable_texts(root: Path) -> list[tuple[Path, str]]:
    dataset_file = root / DATASET_NAME
    if not dataset_file.exists():
        return []
    texts = [(dataset_file, "dataset.json")]
    try:
        cases = load_dataset(root).cases
    except ContractError:
        return texts
    for _case_id, rel in referenced_artifacts(cases):
        path = root / rel
        if path.is_file() and path.stat().st_size < 1_000_000:
            texts.append((path, rel))
    return texts


def snapshot_dataset(dataset_dir: Path,
                      snapshots_root: Path | None = None) -> Path:
    """验证通过后创建不可变快照（独立副本）；未通过验证拒绝。"""
    dataset_dir = Path(dataset_dir)
    dataset = load_dataset(dataset_dir)
    problems = validate_dataset(dataset)
    if problems:
        raise DatasetError("Dataset 未通过验证，拒绝 snapshot："
                           + "；".join(problems))
    digest = artifacts.dataset_digest(dataset_dir)
    base = Path(snapshots_root) if snapshots_root else dataset_dir / ".snapshots"
    target = base / f"{dataset.id}-{digest[len('sha256:'):]}"
    if target.exists():
        return target
    target.mkdir(parents=True)
    shutil.copy2(dataset_dir / DATASET_NAME, target / DATASET_NAME)
    manifest = artifacts.read_manifest(dataset_dir)
    shutil.copy2(dataset_dir / artifacts.MANIFEST_NAME, target / artifacts.MANIFEST_NAME)
    for entry in manifest:
        source = dataset_dir / entry["path"]
        destination = target / entry["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m trace_eval.datasets")
    sub = parser.add_subparsers(dest="command", required=True)
    p_validate = sub.add_parser("validate", help="校验一个 Dataset")
    p_validate.add_argument("dataset_dir")
    p_snapshot = sub.add_parser("snapshot", help="校验并创建不可变快照")
    p_snapshot.add_argument("dataset_dir")
    p_snapshot.add_argument("--to", type=Path, default=None,
                           help="快照根目录（默认 <dataset-dir>/.snapshots）")
    args = parser.parse_args(argv)

    try:
        if args.command == "validate":
            problems = validate_dataset(load_dataset(args.dataset_dir))
            for problem in problems:
                print("✗", problem)
            print("FAIL（%d 项）" % len(problems) if problems else "OK")
            return 1 if problems else 0
        target = snapshot_dataset(args.dataset_dir, args.to)
    except DatasetError as error:
        print("error:", error)
        return 2
    print("snapshot:", target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
