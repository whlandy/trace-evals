#!/usr/bin/env python3
"""确定性轨迹匹配：strict / unordered / subset / superset，并给部分差异。"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trust._recorder import install_recorder_path  # noqa: E402
install_recorder_path()
import trace_schema as ts                         # noqa: E402

MODES = {"strict", "unordered", "subset", "superset"}


def canonical_steps(trace: dict) -> list[dict[str, Any]]:
    """按 entry/next 执行顺序提取语义步骤；nodeId 不参与相等判断。"""
    result = []
    current = ts.meta(trace).get("entry")
    seen = set()
    while current and current not in seen:
        seen.add(current)
        node = trace[current]
        action = node.get("action") or {}
        result.append({
            "nodeId": current,
            "action": action.get("type"),
            "arguments": action.get("param") or {},
            "selector": ts.selector_of(node),
            "assertion": ts.assertion_of(node),
            "responses": ts.expected_responses(node),
            "optional": ts.is_optional(node),
        })
        current = ts.next_of(node)
    return result


def _semantic(step: dict) -> str:
    return json.dumps({k: v for k, v in step.items() if k != "nodeId"},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _lcs(left: list[str], right: list[str]) -> int:
    row = [0] * (len(right) + 1)
    for item in left:
        previous = 0
        for j, other in enumerate(right, 1):
            saved = row[j]
            row[j] = previous + 1 if item == other else max(row[j], row[j - 1])
            previous = saved
    return row[-1]


def match_trajectories(actual: dict, reference: dict, mode: str = "strict") -> dict:
    if mode not in MODES:
        raise ValueError(f"未知 trajectory match mode：{mode!r}")
    actual_steps, reference_steps = canonical_steps(actual), canonical_steps(reference)
    a, r = [_semantic(x) for x in actual_steps], [_semantic(x) for x in reference_steps]
    ac, rc = Counter(a), Counter(r)
    overlap = sum((ac & rc).values())
    missing = []
    remaining = rc - ac
    for step in reference_steps:
        key = _semantic(step)
        if remaining[key] > 0:
            missing.append(step)
            remaining[key] -= 1
    extra = []
    # Consume reference occurrences first, so a repeated action marks the surplus
    # occurrence as extra rather than the earlier valid occurrence.
    remaining = rc.copy()
    for step in actual_steps:
        key = _semantic(step)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            extra.append(step)

    if mode == "strict":
        passed = a == r
        numerator = _lcs(a, r)
        denominator = max(len(a), len(r), 1)
    elif mode == "unordered":
        passed = ac == rc
        numerator, denominator = overlap, max(len(a), len(r), 1)
    elif mode == "subset":
        passed = not extra
        numerator, denominator = overlap, max(len(a), 1)
    else:
        passed = not missing
        numerator, denominator = overlap, max(len(r), 1)

    # 同 action 但语义 payload 不同，单独报参数/selector 不匹配。
    mismatches = []
    for index, (actual_step, reference_step) in enumerate(zip(actual_steps, reference_steps)):
        if (actual_step["action"] == reference_step["action"]
                and _semantic(actual_step) != _semantic(reference_step)):
            mismatches.append({"index": index, "actual": actual_step,
                               "reference": reference_step})

    return {
        "mode": mode,
        "passed": passed,
        "score": round(numerator / denominator, 4),
        "confidence": "deterministic",
        "actualCount": len(a),
        "referenceCount": len(r),
        "matchedCount": overlap,
        "missing": missing,
        "extra": extra,
        "argumentMismatches": mismatches,
        "orderMismatch": ac == rc and a != r,
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法：python3 -m trust.trajectory_match ACTUAL REFERENCE [MODE]")
        return 2
    paths = [Path(x) / "trace.json" if Path(x).is_dir() else Path(x) for x in argv[:2]]
    traces = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    print(json.dumps(match_trajectories(*traces, mode=argv[2] if len(argv) > 2 else "strict"),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
