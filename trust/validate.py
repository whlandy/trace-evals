#!/usr/bin/env python3
"""评估这个评估器 —— 三项可证伪的检查。

方案里写着：**未经校准的可信度分数比没有分数更糟**，因为它会被信任。
所以这里不去凑一个好看的 AUC，只做真正能做的三件事：

  1. 变异单调性   注入缺陷后罚分必须变高（44 对样本）
  2. 命名失败节点 实测标红的轨迹，规则层有没有点出**真正断掉的那个节点**
  3. 排序一致性   实测 stable-green 的轨迹，分数排第几

第 2 项比任何分数都硬：它问的不是「你觉得这条轨迹好不好」，
而是「你指的地方，就是它实际摔倒的地方吗」。

用罚分而不是分数做单调性检验：分数有 0 下限，坏透了的轨迹注入更多缺陷也还是 0，
那不是评估器没抓住，是量尺到头了。

    python3 trust/validate.py <轨迹目录> ...
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trust._recorder import install_recorder_path              # noqa: E402
install_recorder_path()

from trust.mutate import MUTATIONS, mutate                      # noqa: E402
from trust.score import score_trace                             # noqa: E402

LABELS = Path(__file__).resolve().parent / "labels.jsonl"


def load_labels() -> dict[str, dict]:
    if not LABELS.exists():
        return {}
    latest = {}
    for line in LABELS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            latest[record["case"]] = record          # 同一条轨迹以最后一次为准
    return latest


def monotonicity(traces: dict[str, dict]) -> tuple[int, int, list[str]]:
    checked = passed = 0
    failures = []
    for case, trace in traces.items():
        base = score_trace(trace)["penalty"]
        for name in MUTATIONS:
            result = mutate(trace, name)
            if result is None:
                continue
            checked += 1
            after = score_trace(result[0])["penalty"]
            if after > base:
                passed += 1
            else:
                failures.append(f"{case}/{name}: 罚分 {base} → {after}")
    return checked, passed, failures


def named_the_failure(traces: dict[str, dict], labels: dict[str, dict]):
    """实测断在哪个节点，规则层有没有点过那个节点。"""
    rows = []
    for case, record in labels.items():
        if record["label"] not in ("stable-red", "flip"):
            continue
        node = record["runs"][0].get("failedNode")
        trace = traces.get(case)
        if not node or trace is None:
            continue
        flagged = {f["node"] for f in score_trace(trace)["findings"]}
        rows.append((case, node, node in flagged))
    return rows


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    traces = {}
    for arg in argv:
        path = Path(arg)
        path = path / "trace.json" if path.is_dir() else path
        traces[path.parent.name] = json.loads(path.read_text(encoding="utf-8"))
    labels = load_labels()

    checked, passed, failures = monotonicity(traces)
    print(f"① 变异单调性：{passed}/{checked} 对样本罚分变高")
    for line in failures[:6]:
        print(f"     ✗ {line}")

    rows = named_the_failure(traces, labels)
    hit = sum(1 for _, _, ok in rows if ok)   # 只数真的命中的
    print(f"\n② 命名失败节点：{hit}/{len(rows)} 条实测标红的轨迹，"
          f"规则层点出了真正断掉的那个节点")
    for case, node, ok in rows:
        print(f"     {'✓' if ok else '✗'} {case:<12} 实际断在 {node}")

    ranked = sorted(traces, key=lambda c: score_trace(traces[c])["penalty"])
    greens = [c for c, r in labels.items() if r["label"] == "stable-green"]
    print(f"\n③ 排序一致性：罚分从低到高 {ranked}")
    for case in greens:
        print(f"     实测 stable-green 的 {case} 排第 {ranked.index(case) + 1}")
    if len(greens) < 2:
        print("     样本里只有 1 条绿 —— 这一项目前只值 1 bit，不能当校准")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
