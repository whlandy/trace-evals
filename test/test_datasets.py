"""Round 2 验收：版本化 Dataset 与 Artifact 完整性。

逐条守设计文档 Round 2 的验收标准：

1. Dataset 同内容得到相同 digest，任一 Artifact 修改都会改变 digest；
2. 重复 Case ID、路径逃逸、摘要不符和缺失 Artifact 必须失败；
3. 写操作 Case 缺少 state policy 时必须失败；
4. Snapshot 后修改源目录不影响已创建的 Experiment 输入；
5. 至少有一个可公开提交的 smoke Dataset（test/fixtures/contracts/dataset/）。
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from trace_eval import artifacts
from trace_eval.datasets import (
    Dataset, DatasetError, build_manifest, load_dataset,
    snapshot_dataset, validate_dataset,
)

HERE = Path(__file__).resolve().parent / "fixtures" / "contracts"
FIXTURE = HERE / "dataset"
REPO_ROOT = HERE.parents[2]


def _copy_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "ds"
    shutil.copytree(FIXTURE, target)
    return target


def _load_dataset_file(dataset_dir: Path) -> dict:
    return json.loads((dataset_dir / "dataset.json").read_text(encoding="utf-8"))


def _save_dataset_file(dataset_dir: Path, value: dict) -> None:
    (dataset_dir / "dataset.json").write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 5) 公开 smoke Dataset 可加载、可校验 ───────────────────────────


def test_smoke_dataset_loads_and_validates():
    dataset = load_dataset(FIXTURE)
    assert dataset.id == "contracts-smoke"
    assert {case.id for case in dataset.cases} == {"case-a", "case-b"}
    assert validate_dataset(dataset) == []


def test_smoke_dataset_has_registered_manifest():
    entries = {entry["path"]: entry for entry in artifacts.read_manifest(FIXTURE)}
    assert set(entries) == {
        "case-a/golden.json", "case-a/recording.json", "case-b/recording.json"}
    assert all(e["digest"].startswith("sha256:") for e in entries.values())


# ── 1) 同内容同 digest；任一 Artifact 修改 digest 变 ───────────────


def test_same_content_same_digest_artifact_change_changes_digest(tmp_path):
    a = _copy_fixture(tmp_path)
    b = tmp_path / "copy"
    shutil.copytree(FIXTURE, b)
    digest_a = artifacts.dataset_digest(a)
    assert artifacts.dataset_digest(b) == digest_a

    (a / "case-a" / "golden.json").write_text(
        (a / "case-a" / "golden.json").read_text(encoding="utf-8") + " ",
        encoding="utf-8")
    build_manifest(a)  # 重新登记 → manifest 摘要变
    assert artifacts.dataset_digest(a) != digest_a


# ── 2) 各类违例必须失败 ───────────────────────────────────────────


def test_duplicate_case_id_fails(tmp_path):
    ds = _copy_fixture(tmp_path)
    value = _load_dataset_file(ds)
    value["cases"].append(dict(value["cases"][0]))  # 复制出同 ID Case
    _save_dataset_file(ds, value)
    problems = validate_dataset(load_dataset(ds))
    assert any("重复 Case ID" in p for p in problems)


def test_path_escape_fails(tmp_path):
    ds = _copy_fixture(tmp_path)
    value = _load_dataset_file(ds)
    value["cases"][0]["input"]["golden"] = "../outside.json"
    _save_dataset_file(ds, value)
    problems = validate_dataset(load_dataset(ds))
    assert any("逃逸" in p for p in problems)


def test_absolute_path_fails(tmp_path):
    ds = _copy_fixture(tmp_path)
    value = _load_dataset_file(ds)
    value["cases"][0]["input"]["golden"] = "/etc/hostname"
    _save_dataset_file(ds, value)
    problems = validate_dataset(load_dataset(ds))
    assert any("相对路径" in p for p in problems)


def test_digest_mismatch_fails(tmp_path):
    ds = _copy_fixture(tmp_path)
    (ds / "case-b" / "recording.json").write_text(
        '{"steps": [{"id": "tampered"}]}', encoding="utf-8")
    problems = validate_dataset(load_dataset(ds))
    assert any("摘要不符" in p for p in problems)


def test_missing_artifact_fails(tmp_path):
    ds = _copy_fixture(tmp_path)
    (ds / "case-a" / "recording.json").unlink()
    problems = validate_dataset(load_dataset(ds))
    assert any("Artifact 缺失" in p for p in problems)


def test_unregistered_artifact_fails(tmp_path):
    ds = _copy_fixture(tmp_path)
    (ds / "case-c").mkdir()
    (ds / "case-c" / "recording.json").write_text('{"steps": []}', encoding="utf-8")
    value = _load_dataset_file(ds)
    value["cases"].append({"id": "case-c", "executor": "web",
                          "input": {"recording": "case-c/recording.json"}})
    _save_dataset_file(ds, value)
    problems = validate_dataset(load_dataset(ds))
    assert any("未登记 manifest" in p for p in problems)


def test_unknown_dataset_major_version_rejected(tmp_path):
    ds = _copy_fixture(tmp_path)
    value = _load_dataset_file(ds)
    value["schema"] = "trace-evals.dataset/v2"
    _save_dataset_file(ds, value)
    with pytest.raises(DatasetError, match="未知主版本"):
        load_dataset(ds)


def test_secret_in_dataset_fails(tmp_path):
    ds = _copy_fixture(tmp_path)
    value = _load_dataset_file(ds)
    value["metadata"]["note"] = "key sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    _save_dataset_file(ds, value)
    problems = validate_dataset(load_dataset(ds))
    assert any("Secret" in p for p in problems)


def test_bad_executor_rejected(tmp_path):
    ds = _copy_fixture(tmp_path)
    value = _load_dataset_file(ds)
    value["cases"][0]["executor"] = "appium"
    _save_dataset_file(ds, value)
    problems = validate_dataset(load_dataset(ds))
    assert any("executor" in p for p in problems)


# ── 3) 写操作 Case 缺 state policy 必须失败 ───────────────────────


def test_write_case_without_state_policy_fails(tmp_path):
    ds = _copy_fixture(tmp_path)
    value = _load_dataset_file(ds)
    del value["cases"][1]["environment"]["statePolicy"]
    _save_dataset_file(ds, value)
    problems = validate_dataset(load_dataset(ds))
    assert any("statePolicy" in p for p in problems)


def test_write_case_with_state_policy_passes():
    dataset = load_dataset(FIXTURE)
    case_b = next(c for c in dataset.cases if c.id == "case-b")
    assert case_b.environment["statePolicy"]
    assert validate_dataset(dataset) == []


# ── 4) Snapshot 隔离性与准入 ──────────────────────────────────────


def test_snapshot_is_isolated_from_source_changes(tmp_path):
    ds = _copy_fixture(tmp_path)
    snap = snapshot_dataset(ds, tmp_path / "snaps")
    (ds / "case-a" / "golden.json").write_text("tampered", encoding="utf-8")
    assert artifacts.file_digest(snap / "case-a" / "golden.json") == \
        artifacts.file_digest(FIXTURE / "case-a" / "golden.json")
    assert validate_dataset(load_dataset(snap)) == []


def test_snapshot_refuses_invalid_dataset(tmp_path):
    ds = _copy_fixture(tmp_path)
    value = _load_dataset_file(ds)
    value["cases"].append(dict(value["cases"][0]))
    _save_dataset_file(ds, value)
    with pytest.raises(DatasetError, match="未通过验证"):
        snapshot_dataset(ds, tmp_path / "snaps")


def test_snapshot_idempotent_by_digest(tmp_path):
    ds = _copy_fixture(tmp_path)
    first = snapshot_dataset(ds, tmp_path / "snaps")
    assert snapshot_dataset(ds, tmp_path / "snaps") == first


# ── round-trip 与 CLI ─────────────────────────────────────────────


def test_dataset_roundtrip_lossless():
    dataset = load_dataset(FIXTURE)
    again = Dataset.from_dict(
        json.loads(json.dumps(dataset.to_dict(), ensure_ascii=False)))
    assert again.to_dict() == dataset.to_dict()


def test_cli_validate_ok():
    result = subprocess.run(
        [sys.executable, "-m", "trace_eval.datasets", "validate", str(FIXTURE)],
        capture_output=True, text=True, cwd=REPO_ROOT)
    assert result.returncode == 0
    assert "OK" in result.stdout


def test_cli_validate_fails_with_code_1_on_escape(tmp_path):
    ds = _copy_fixture(tmp_path)
    value = _load_dataset_file(ds)
    value["cases"][0]["input"]["golden"] = "../outside.json"
    _save_dataset_file(ds, value)
    result = subprocess.run(
        [sys.executable, "-m", "trace_eval.datasets", "validate", str(ds)],
        capture_output=True, text=True, cwd=REPO_ROOT)
    assert result.returncode == 1
    assert "逃逸" in result.stdout
