"""Round 0 契约基线：Golden Result 冻结 + 确定性 + Fixture 卫生。

守住三件事：

1. `audit()` 与 `maa_execution.evaluate()` 对固定输入的结果与
   `golden-results/` 逐字节一致 —— 后续任何 Round 无意改变评分语义都会在这里变红；
2. 同一输入连续运行两次得到完全相同的结果（字符级）；
3. Fixture 不含 Secret、真实 IP、账号或 Cookie。

覆盖场景：成功、失败、optional skip、digest mismatch、incomplete Golden。
重建基线：`python3 test/fixtures/contracts/generate.py`（会改变 Golden Result，
必须人工确认评分语义确实有意变化）。
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

from trust.audit import audit
from trust.maa_execution import EvaluationError, evaluate
from trust.stable_json import dumps_stable, stable_roundtrip

HERE = Path(__file__).resolve().parent / "fixtures" / "contracts"
RESULTS = HERE / "golden-results"
MAA = HERE / "maa"
REPO_ROOT = HERE.parents[2]


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ── audit() Golden Result 与确定性 ─────────────────────────────────


@pytest.mark.parametrize("case, golden", [
    ("case-success", "audit-case-success.json"),
    ("case-weak", "audit-case-weak.json"),
])
def test_audit_matches_golden_and_is_byte_deterministic(case, golden):
    first = audit(HERE / "cases" / case)
    second = audit(HERE / "cases" / case)
    assert dumps_stable(first) == dumps_stable(second), "两次运行必须逐字节一致"
    assert first == _load(RESULTS / golden)
    assert dumps_stable(first) == (RESULTS / golden).read_text(encoding="utf-8").strip()


def test_weak_case_is_actually_flagged():
    """零断言用例必须真的报出发现项 —— 否则这个 Fixture 是空的。"""
    result = audit(HERE / "cases" / "case-weak")
    assert result["findings"]
    failures = {f["failure"] for f in result["findings"]}
    assert failures & {"weak", "silent-pass"}
    assert result["hasRecording"] is False


# ── Maa 执行场景 ───────────────────────────────────────────────────


def _golden():
    return _load(MAA / "golden.json")


def _exec(name: str):
    return _load(MAA / name)


def test_maa_success_matches_golden():
    result = evaluate(_golden(), _exec("execution-success.json"))
    assert result == _load(RESULTS / "maa-success.json")
    assert result["taskSuccess"] is True
    assert result["failedNodeIds"] == []


def test_maa_failure_reports_failed_node():
    result = evaluate(_golden(), _exec("execution-fail.json"))
    assert result == _load(RESULTS / "maa-fail.json")
    assert result["taskSuccess"] is False
    assert result["failedNodeIds"] == ["step_0002"]


def test_maa_optional_skip_is_satisfied():
    """optional 节点被 skip 应当算满足，而不是失败。"""
    result = evaluate(_golden(), _exec("execution-optional-skip.json"))
    assert result == _load(RESULTS / "maa-optional-skip.json")
    assert result["taskSuccess"] is True


def test_digest_mismatch_rejected_with_stable_message():
    execution = _exec("execution-success.json")
    execution["golden"] = dict(execution["golden"], digest="sha256:" + "0" * 64)
    with pytest.raises(EvaluationError) as excinfo:
        evaluate(_golden(), execution)
    assert str(excinfo.value) == (RESULTS / "maa-digest-mismatch.err").read_text(encoding="utf-8").strip()


def test_incomplete_golden_rejected_with_stable_message():
    golden = _golden()
    golden["$meta"]["attach"]["status"] = "incomplete"
    with pytest.raises(EvaluationError) as excinfo:
        evaluate(golden, _exec("execution-success.json"))
    assert str(excinfo.value) == (RESULTS / "maa-incomplete-golden.err").read_text(encoding="utf-8").strip()


# ── Fixture 卫生：无 Secret / 真实 IP / 账号 / Cookie ────────────────

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)cookie\s*[:=]"),
    re.compile(r"(?i)(password|passwd|token)\s*[:=]\s*['\"][^'\"]{6,}"),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
]


def test_fixtures_contain_no_sensitive_material():
    for path in sorted(HERE.rglob("*")):
        if path.is_file() and path.suffix in {".json", ".err"}:
            text = path.read_text(encoding="utf-8")
            for pattern in SECRET_PATTERNS:
                assert not pattern.search(text), f"{path.name} 命中敏感形态: {pattern.pattern}"


# ── 基线元数据与场景覆盖 ───────────────────────────────────────────


def test_baseline_metadata_and_scenario_coverage():
    baseline = _load(HERE / "BASELINE.json")
    assert re.fullmatch(r"[0-9a-f]{40}", baseline["recorderSha"])
    assert isinstance(baseline["tests"], int) and baseline["tests"] > 0
    names = {p.stem for p in MAA.glob("execution-*.json")}
    assert {"execution-success", "execution-fail", "execution-optional-skip"} <= names


def test_recorder_sha_not_drifted():
    """recorder 移动后必须重跑 generate.py 重新冻结基线 —— 这里红得有理由。"""
    current = subprocess.run(
        ["git", "-C", str(REPO_ROOT.parent / "edr-cloud-recorder"), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()
    recorded = _load(HERE / "BASELINE.json")["recorderSha"]
    assert recorded == current, (
        f"recorder 已从基线 {recorded[:12]} 移动到 {current[:12]} —— "
        "评分输入的形状可能已变化：确认语义后重跑 test/fixtures/contracts/generate.py")


# ── stable_json 自身契约 ───────────────────────────────────────────


def test_stable_json_order_independence_and_roundtrip():
    a = {"b": 1, "a": [1, 2, {"y": 1, "x": 2}]}
    b = {"a": [1, 2, {"x": 2, "y": 1}], "b": 1}
    assert dumps_stable(a) == dumps_stable(b)
    assert stable_roundtrip(a) == a
