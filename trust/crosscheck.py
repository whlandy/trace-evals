#!/usr/bin/env python3
"""拿 recording.json 和 trace.json 对照。

**为什么必须有这一层**：有一类缺陷在产物里根本不可见。
第 3 轮的咬合测试当场证明了这件事 —— 把「可选」标记摘掉之后，轨迹从表面上看
是干净的，规则层完全看不见。录制里有的事实，编译进轨迹时可能丢掉、也可能
被改写；只看轨迹就永远发现不了「丢了什么」。

对照靠 provenance.sourceStepId 把节点连回录制步骤。

    python3 trust/crosscheck.py <轨迹目录> ...
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trust._recorder import install_recorder_path              # noqa: E402
install_recorder_path()

import trace_schema as ts                                      # noqa: E402
from trust.rules import EVIDENCE, LOUD_LATER, OBSERVE, SILENT_PASS, _finding  # noqa: E402


def _by_source(trace: dict) -> dict[str, tuple[str, dict]]:
    index = {}
    for node_id, node in ts.nodes(trace):
        source = ts.provenance(node).get("sourceStepId")
        if source:
            index[source] = (node_id, node)
    return index


def crosscheck(recording: dict, trace: dict) -> list[dict]:
    steps = recording.get("steps") or []
    index = _by_source(trace)
    findings = []

    for step in steps:
        entry = index.get(step.get("id"))
        if not entry:
            continue
        node_id, node = entry

        # 录制器验过唯一性、也记下了撞几个，但 _selector() 不搬这两个字段。
        # 轨迹里只剩 .first() 这个症状，而症状看不出「撞了几个」，
        # 更看不出**根本没验成**（matches 为「未知」的那种）。
        if step.get("ambiguous"):
            findings.append(_finding(
                "ambiguity_fact_lost", OBSERVE, LOUD_LATER, node_id,
                f"录制记着 ambiguous / matches={step.get('matches')!r}，轨迹里没有",
                "回放只能看到 .first()，看不出撞了几个、也看不出是不是压根没验成"))

        # 录制里的密码步骤本该在编译时被切掉。它出现在轨迹里就是凭据外泄。
        if step.get("secret") and step.get("id") in index:
            findings.append(_finding(
                "secret_step_in_trace", EVIDENCE, SILENT_PASS, node_id,
                "录制标了 secret 的步骤出现在轨迹里",
                "凭据会随轨迹一起流转；而且回放会把录制时的值再填一遍"))

    # 掉步：录制里有、轨迹里没有。登录前缀是**故意**切掉的，不算掉步 ——
    # 所以只看第一个进了轨迹的步骤之后的那一段。
    kept = [i for i, s in enumerate(steps) if s.get("id") in index]
    if kept:
        tail = steps[kept[0]:]
        dropped = [s for s in tail if s.get("id") not in index]
        if dropped:
            kinds = sorted({s.get("type") for s in dropped})
            findings.append(_finding(
                "steps_dropped_in_compile", OBSERVE, LOUD_LATER, None,
                f"录制中段有 {len(dropped)} 步没进轨迹（类型：{kinds}）",
                "回放会跳过这些操作，却仍按成功计分 —— 少做一步也报绿"))

    return findings


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    report = []
    for arg in argv:
        case = Path(arg)
        raw, golden = case / "recording.json", case / "trace.json"
        if not raw.exists():
            print(f"跳过 {case.name}：没有 recording.json，对照做不了")
            continue
        findings = crosscheck(
            json.loads(raw.read_text(encoding="utf-8")),
            json.loads(golden.read_text(encoding="utf-8")))
        report.append({"case": case.name, "findings": findings})
        print(f"{case.name:<24} {len(findings)} 条")
        for f in findings:
            print(f"    [{f['rule']}] {f['node'] or '整条轨迹'}: {f['evidence']}")

    out = Path(__file__).resolve().parent / "crosscheck.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
