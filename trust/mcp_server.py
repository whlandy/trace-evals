"""
trust/mcp_server.py — trace-eval 的 MCP server（黑盒结论出口）。

把「这条 trace 值不值得信」暴露成 MCP 工具，让 eco（编排层）以及任何 MCP
客户端都能直接拿到评估结论，而不必去读命令行输出或文件。返回一律是
`json.dumps(..., ensure_ascii=False)` 的字符串（与 edr-wd 的 FastMCP server 契约一致）。

设计约定（与 trust/audit.py 的哲学一致）：
  * 分数只表达排序（uncalibrated-ordering-only / uncalibrated-judge-score），不是概率。
  * 结论按失败形态分类（silent-pass/flaky/loud-later/weak）而非拍严重度。
  * 缺运行观测时必须显式 `unknown`，不得用 0.5/LLM 猜测填空。

启动：
  python3 -m trust.mcp_server            # stdio（默认，供 eco 通过 MCP 客户端连）
  python3 -m trust.mcp_server --http --host 127.0.0.1 --port 8790
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

# 让本仓库顶层可被 import（trust.* 与外部路径无关，但保证脚本可独立执行）。
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from trust.audit import audit  # noqa: E402
from trust.score import score_trace  # noqa: E402
from trust.step_score import score_steps  # noqa: E402

mcp = FastMCP("trace-audit")

_AXIS_TITLE = {"replay": "replayability", "evidence": "evidential-power",
               "observe": "observability"}


def _finding_dict(finding: dict) -> dict:
    return {
        "rule": finding.get("rule"),
        "axis": _AXIS_TITLE.get(finding.get("axis"), finding.get("axis")),
        "failure": finding.get("failure"),
        "node": finding.get("node"),
        "evidence": finding.get("evidence"),
        "consequence": finding.get("consequence"),
    }


def _audit_payload(case: Path) -> dict[str, Any]:
    result = audit(case)
    findings = result.get("findings", [])
    crosscheck = result.get("crosscheck", [])
    all_findings = findings + crosscheck

    by_failure: dict[str, int] = {}
    for f in all_findings:
        key = f.get("failure") or "unknown"
        by_failure[key] = by_failure.get(key, 0) + 1
    by_axis: dict[str, int] = {}
    for f in all_findings:
        key = f.get("axis") or "unknown"
        by_axis[_AXIS_TITLE.get(key, key)] = by_axis.get(
            _AXIS_TITLE.get(key, key), 0) + 1

    payload = {
        "case": case.name,
        "steps": result.get("steps"),
        "score": result.get("score"),
        "penalty": result.get("penalty"),
        "confidence": result.get("confidence"),
        "label": result.get("label"),
        "has_recording": result.get("hasRecording"),
        "finding_count": len(all_findings),
        "by_failure_shape": by_failure,
        "by_axis": by_axis,
        "findings": [_finding_dict(f) for f in all_findings],
        # 黑盒结论：有没有「悄悄绿」——做错了照样报成功，最伤的一种。
        "silent_pass": "silent-pass" in by_failure,
        # 契约自白：分数是排序不是概率。
        "calibration": "uncalibrated-ordering-only",
    }
    return payload


@mcp.tool(
    name="audit_trace",
    description=(
        "Judge whether a recorded automation trace is worth trusting at all. "
        "Answer: given a case directory that contains trace.json (v2, with a $meta node) "
        "and optionally recording.json, is this green result believable? Returns score "
        "(ordering-only, not a probability), penalty, failure-shape counts "
        "(silent-pass/flaky/loud-later/weak), per-axis findings with evidence, and a "
        "silent_pass flag. A silent-pass finding means the trace can succeed while doing "
        "nothing correct — worse than failing. Non-v2 input is rejected; export first "
        "via desktop_to_v2."
    ),
)
def audit_trace(case_dir: str) -> str:
    case = Path(case_dir)
    if not case.is_dir():
        return json.dumps({"ok": False, "error": f"not a directory: {case_dir}"},
                          ensure_ascii=False)
    trace_candidate = case / "trace.json"
    if not trace_candidate.is_file() and (case / "golden-trace.json").is_file():
        return json.dumps({
            "ok": False,
            "error": "case has golden-trace.json but no v2 trace.json — export first: "
                     "desktop_to_v2.py <case>",
        }, ensure_ascii=False)
    try:
        payload = _audit_payload(case)
    except Exception as exc:  # noqa: BLE001 - surface as black-box error
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                          ensure_ascii=False)
    payload["ok"] = True
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool(
    name="step_score_trace",
    description=(
        "Per-step five-dimension scoring of a trace (action correctness, argument quality, "
        "context fit, evidence quality, replay safety). Input: a path to a v2 trace.json "
        "file. Returns a dict keyed by step id with the per-dimension verdicts."
    ),
)
def step_score_trace(trace_path: str) -> str:
    path = Path(trace_path)
    if not path.is_file():
        return json.dumps({"ok": False, "error": f"not a file: {trace_path}"},
                          ensure_ascii=False)
    try:
        trace = json.loads(path.read_text(encoding="utf-8"))
        payload = score_steps(trace)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                          ensure_ascii=False)
    payload["ok"] = True
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool(
    name="audit_contract",
    description=(
        "Describes what this evaluator's outputs mean, so a caller never mistakes an "
        "ordering-only score for a probability. Returns the calibration caveat and the "
        "failure-shape taxonomy."
    ),
)
def audit_contract() -> str:
    return json.dumps({
        "ok": True,
        "name": "trace-audit",
        "scores_are": "uncalibrated-ordering-only (not probabilities)",
        "axes": ["replayability", "evidential-power", "observability"],
        "failure_shapes": {
            "silent-pass": "wrong yet reported success — false confidence",
            "flaky": "outcome flips with start state",
            "loud-later": "green now, red later",
            "weak": "never fails but proves nothing",
        },
        "hard_rule": "a missing runtime observation is 'unknown', never a guess",
    }, ensure_ascii=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="trace-audit MCP server")
    parser.add_argument("--http", action="store_true", help="Run in HTTP mode")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    if args.http:
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
