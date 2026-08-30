#!/usr/bin/env python3
"""把发现项汇成一个分数 —— 并且把「这个分数还没被校准」写在脸上。

分数 = 100 减去各发现项的罚分，下限 0。罚分按**失败形态**给，不按规则给：

    silent-pass  25   悄悄绿：做错了照样报成功。它会变成虚假的信心，
                      比失败更伤 —— 失败至少你知道要去查
    loud-later   15   当时不报以后才炸：会真的失败，只是延迟发作
    flaky        10   时好时坏：结论不稳定，但至少不会一直骗你
    weak          5   不会失败，也说明不了什么

**这四个数字是拍脑袋的，只有相对顺序有依据。** 校准需要一批「稳定绿 / 翻转 /
稳定红」都足够的标签，而 P1 实测出来的是 1 绿 6 红 —— 这个比例算不出校准误差。
所以这里只承诺**排序**，不承诺概率；`confidence` 字段把这件事明说出来。

    python3 trust/score.py <轨迹目录> ...
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trust._recorder import install_recorder_path              # noqa: E402
install_recorder_path()

from trust.rules import FLAKY, LOUD_LATER, SILENT_PASS, WEAK, trace_findings  # noqa: E402

PENALTY = {SILENT_PASS: 25, LOUD_LATER: 15, FLAKY: 10, WEAK: 5}

# 分数只表达排序。说成概率就是在撒谎 —— 标签比例算不出校准误差。
CONFIDENCE = "uncalibrated-ordering-only"


def score_trace(trace: dict) -> dict:
    result = trace_findings(trace)
    penalty = sum(PENALTY[f["failure"]] for f in result["findings"])
    result["penalty"] = penalty
    result["score"] = max(0, 100 - penalty)
    result["confidence"] = CONFIDENCE
    return result


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    rows = []
    for arg in argv:
        path = Path(arg)
        path = path / "trace.json" if path.is_dir() else path
        result = score_trace(json.loads(path.read_text(encoding="utf-8")))
        rows.append((path.parent.name, result))
    rows.sort(key=lambda r: r[1]["penalty"])

    # 罚分也要打出来：分数有下限，坏到一定程度就都是 0，排序被抹平；
    # 罚分不截断，才看得出「更坏」和「坏透了」的区别。
    print("%-24s %6s %6s %6s  %s" % ("轨迹", "分数", "罚分", "发现", "最坏的一条"))
    for case, result in rows:
        worst = next((f for f in result["findings"] if f["failure"] == SILENT_PASS),
                     None) or (result["findings"][0] if result["findings"] else None)
        print("%-24s %6d %6d %6d  %s" % (
            case[:24], result["score"], result["penalty"], len(result["findings"]),
            f"[{worst['rule']}] {worst['evidence'][:44]}" if worst else "—"))
    print(f"\n置信度：{CONFIDENCE} —— 分数只表达排序，不是概率")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
