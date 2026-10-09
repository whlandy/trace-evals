#!/usr/bin/env python3
"""Round 0 基线 Fixture 与 Golden Result 生成器（一次性重跑，产物进 Git）。

用法（在仓库根目录）：

    python3 test/fixtures/contracts/generate.py

产物：

    test/fixtures/contracts/cases/case-success/   audit() 的干净用例（带 recording）
    test/fixtures/contracts/cases/case-weak/      audit() 的零断言用例（弱证据）
    test/fixtures/contracts/maa/golden.json       Maa Golden（含一个 optional 节点）
    test/fixtures/contracts/maa/execution-*.json  三种执行（success/fail/optional-skip）
    test/fixtures/contracts/golden-results/*.json 两个函数的 Golden Result
    test/fixtures/contracts/golden-results/*.err  两个错误场景的确定性报错文本
    test/fixtures/contracts/BASELINE.json         测试基线 + recorder SHA 锁定

重跑条件：recorder 的 trace_v1/trace_schema 或 trust/ 评分逻辑有意变化后。
重跑会改变 Golden Result —— 那本身就是"评分语义变了"的信号，必须人工确认。
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test"))

from trust._recorder import install_recorder_path          # noqa: E402
install_recorder_path(with_tests=True)

from trace_v1 import to_v2                                 # noqa: E402
from trust.audit import audit                              # noqa: E402
from trust.maa_execution import EvaluationError, evaluate, trace_digest  # noqa: E402
from trust.stable_json import dumps_stable                 # noqa: E402

HERE = Path(__file__).resolve().parent


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, str):
        path.write_text(value, encoding="utf-8")
    else:
        path.write_text(dumps_stable(value) + "\n", encoding="utf-8")


def _audit_case(node_specs: list[dict], name: str) -> dict:
    """用录制器自己的平铺→v2 翻译器建轨迹 —— 不手抄 v2 嵌套布局。"""
    steps, ids = {}, []
    for i, spec in enumerate(node_specs, 1):
        node_id = f"step_{i:04d}"
        ids.append(node_id)
        steps[node_id] = {"status": "ready", **spec}
    for i, node_id in enumerate(ids):
        steps[node_id]["next"] = ids[i + 1] if i + 1 < len(node_specs) else None
    return to_v2({"schema": "edr.success-trace/v1", "name": name, "status": "ready",
                  "entry": ids[0], "steps": steps})


def _maa_golden() -> dict:
    """两个节点：step_0002 声明 optional —— 覆盖 optional-skip 场景。"""
    node1 = {"recognition": {"type": "TemplateMatch", "param": {"template": "a.png"}},
             "action": {"type": "Click", "param": {}}, "next": ["step_0002"], "attach": {}}
    node2 = {"recognition": {"type": "TemplateMatch", "param": {"template": "b.png"}},
             "action": {"type": "Click", "param": {}}, "next": [],
             "attach": {"provenance": {"optional": True, "source": "fixture"}}}
    meta = {"recognition": {"type": "DirectHit", "param": {}},
            "action": {"type": "DoNothing", "param": {}}, "next": [],
            "attach": {"schema": "edr.success-trace/v2", "status": "ready",
                       "nodeOrder": ["step_0001", "step_0002"]}}
    return {"step_0001": node1, "step_0002": node2, "$meta": meta}


def _maa_execution(golden: dict, status: str, step2_status: str) -> dict:
    return {"schema": "edr.maa-execution-trace/v1",
            "golden": {"digest": trace_digest(golden),
                       "nodeOrder": ["step_0001", "step_0002"]},
            "status": status,
            "steps": [
                {"nodeId": "step_0001", "status": "success",
                 "actualAction": "Click", "matchScore": 0.92, "retries": 0},
                {"nodeId": "step_0002", "status": step2_status,
                 "actualAction": "Click", "matchScore": 0.90, "retries": 0},
            ]}


def main() -> int:
    # ── audit() 用例 ────────────────────────────────────────────────
    clean = _audit_case([
        {"selector": {"sel": 'locator("#policy-form")', "kind": "scoped"},
         "action": {"type": "InputText", "param": {"text": "baseline-policy"}}},
        {"selector": {"sel": 'locator("button", { hasText: "保存" })', "kind": "scoped"},
         "action": {"type": "Click", "param": {}},
         "recognition": {"templates": {"element": "save-button.png"}}},
        {"selector": {"sel": 'locator(".eui_tag", { hasText: "已启用" })', "kind": "text"},
         "action": {"type": "Assert",
                    "param": {"assertion": "visible", "expected": "visible"}}},
    ], "contract-clean")
    weak = _audit_case([
        {"selector": {"sel": 'locator("#policy-form")', "kind": "scoped"},
         "action": {"type": "InputText", "param": {"text": "baseline-policy"}}},
        {"selector": {"sel": 'locator("button", { hasText: "保存" })', "kind": "scoped"},
         "action": {"type": "Click", "param": {}}},
    ], "contract-weak")

    cases_dir = HERE / "cases"
    _write(cases_dir / "case-success" / "trace.json", clean)
    _write(cases_dir / "case-success" / "recording.json", {"steps": []})
    _write(cases_dir / "case-weak" / "trace.json", weak)

    # ── Maa 场景 ────────────────────────────────────────────────────
    golden = _maa_golden()
    maa_dir = HERE / "maa"
    _write(maa_dir / "golden.json", golden)
    _write(maa_dir / "execution-success.json",
           _maa_execution(golden, "success", "success"))
    _write(maa_dir / "execution-fail.json",
           _maa_execution(golden, "success", "failure"))
    _write(maa_dir / "execution-optional-skip.json",
           _maa_execution(golden, "success", "skipped"))

    # ── Golden Results（先跑一遍，落盘；随后基线测试逐字节比对）────────
    results = HERE / "golden-results"
    audit_success = audit(cases_dir / "case-success")
    audit_weak = audit(cases_dir / "case-weak")
    _write(results / "audit-case-success.json", audit_success)
    _write(results / "audit-case-weak.json", audit_weak)

    golden_loaded = json.loads((maa_dir / "golden.json").read_text(encoding="utf-8"))

    def _load_exec(name: str) -> dict:
        return json.loads((maa_dir / name).read_text(encoding="utf-8"))

    _write(results / "maa-success.json", evaluate(golden_loaded, _load_exec("execution-success.json")))
    _write(results / "maa-fail.json", evaluate(golden_loaded, _load_exec("execution-fail.json")))
    _write(results / "maa-optional-skip.json",
           evaluate(golden_loaded, _load_exec("execution-optional-skip.json")))

    # 两个错误场景：记录确定性报错文本，而不是假装能评分
    bad_digest = _load_exec("execution-success.json")
    bad_digest["golden"] = dict(bad_digest["golden"], digest="sha256:0" * 1 + "0" * 63)
    try:
        evaluate(golden_loaded, bad_digest)
        raise AssertionError("digest mismatch 应当被拒绝")
    except EvaluationError as error:
        _write(results / "maa-digest-mismatch.err", str(error))
    incomplete = json.loads(dumps_stable(golden_loaded))
    incomplete["$meta"]["attach"]["status"] = "incomplete"
    try:
        evaluate(incomplete, _load_exec("execution-success.json"))
        raise AssertionError("incomplete golden 应当被拒绝")
    except EvaluationError as error:
        _write(results / "maa-incomplete-golden.err", str(error))

    # ── 公开 smoke Dataset（Round 2）────────────────────────────────
    import shutil
    from trace_eval.datasets import SCHEMA_DATASET, build_manifest
    ds = HERE / "dataset"
    if ds.exists():
        shutil.rmtree(ds)
    (ds / "case-a").mkdir(parents=True)
    (ds / "case-b").mkdir(parents=True)
    _write(ds / "case-a" / "golden.json",
           json.loads((maa_dir / "golden.json").read_text(encoding="utf-8")))
    _write(ds / "case-a" / "recording.json", {"steps": []})
    _write(ds / "case-b" / "recording.json",
           {"steps": [{"id": "web-step-1", "type": "click"}]})
    _write(ds / "dataset.json", {
        "schema": SCHEMA_DATASET,
        "id": "contracts-smoke",
        "version": "2026.10",
        "cases": [
            {"id": "case-a", "executor": "maa",
             "input": {"golden": "case-a/golden.json",
                       "recording": "case-a/recording.json"},
             "tags": ["contract"]},
            {"id": "case-b", "executor": "web",
             "input": {"recording": "case-b/recording.json"},
             "environment": {"profile": "local-dev",
                             "statePolicy": {"reset": "per-trial",
                                              "cleanup": "restore-default"},
                             "sideEffects": ["write:policy"]},
             "tags": ["contract", "write"]},
        ],
        "metadata": {"note": "公开 smoke dataset：全合成数据，无敏感内容"},
    })
    build_manifest(ds)

    # ── BASELINE ─────────────────────────────────────────────────────
    import subprocess
    recorder_sha = subprocess.run(
        ["git", "-C", str(ROOT.parent / "edr-cloud-recorder"), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()
    trace_eval_sha = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()
    _write(HERE / "BASELINE.json", {
        "schema": "trace-eval.baseline/v1",
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tests": 288,
        "note": "288 = 建立基线时全量测试数（不含本契约基线新增的测试）",
        "traceEvalSha": trace_eval_sha,
        "recorderSha": recorder_sha,
    })

    print("fixtures + golden results + BASELINE.json 已生成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
