#!/usr/bin/env python3
"""一条轨迹的可信度体检 —— 把四层合成一份报告。

    python3 trust/audit.py recordings/my-flow

报告分两栏，对应两个不同的问题：

    可回放性   再跑一次还会绿吗
    证据力     它绿了能说明什么

第二栏是最容易被忽略的：回放满分也可能来自一条什么都没断言的轨迹。
实测语料里 7 条有 5 条一条断言都没有。

分数只表达排序，不是概率 —— 见 score.py 的说明。**逐条证据才是主产物**：
它指得到具体节点、说得出会发生什么，而分数只是摘要。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trust._recorder import install_recorder_path              # noqa: E402
install_recorder_path()

from trust.crosscheck import crosscheck                        # noqa: E402
from trust.score import score_trace                            # noqa: E402
from trust.validate import load_labels                         # noqa: E402

AXIS_TITLE = {"replay": "可回放性", "evidence": "证据力", "observe": "观测充分性"}
FAILURE_TITLE = {
    "silent-pass": "悄悄绿（做错了照样报成功）",
    "flaky": "时好时坏（换个起点结论就变）",
    "loud-later": "当时不报，以后才炸",
    "weak": "不会失败，也说明不了什么",
}


def audit(case: Path) -> dict:
    trace = json.loads((case / "trace.json").read_text(encoding="utf-8"))
    result = score_trace(trace)
    raw = case / "recording.json"
    result["crosscheck"] = crosscheck(
        json.loads(raw.read_text(encoding="utf-8")), trace) if raw.exists() else []
    # 录制丢了就少一整类检查（编译时丢掉的事实看不见了），这件事要说出来
    result["hasRecording"] = raw.exists()
    result["label"] = (load_labels().get(case.name) or {}).get("label")
    return result


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for arg in argv:
        case = Path(arg)
        result = audit(case)
        print(f"\n══ {case.name} ══  {result['steps']} 步 · "
              f"分数 {result['score']}（罚分 {result['penalty']}）")
        print(f"   {result['confidence']} —— 分数只表达排序，不是概率")
        if result["label"]:
            print(f"   实测标签：{result['label']}")
        if not result["hasRecording"]:
            print("   ⚠ 没有 recording.json：编译时丢掉的事实这一整类检查做不了")

        findings = result["findings"] + result["crosscheck"]
        for axis in ("replay", "evidence", "observe"):
            group = [f for f in findings if f["axis"] == axis]
            if not group:
                continue
            print(f"\n   ── {AXIS_TITLE[axis]}（{len(group)} 条）──")
            for f in sorted(group, key=lambda f: f["failure"]):
                where = f["node"] or "整条轨迹"
                print(f"   [{FAILURE_TITLE[f['failure']]}] {where}")
                print(f"       {f['evidence']}")
                print(f"       → {f['consequence']}")
        if not findings:
            print("\n   没有发现项。注意这**不等于**可信 —— "
                  "只说明在册的规则都没命中。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
