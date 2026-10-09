#!/usr/bin/env python3
"""Round 5：统一 Aggregator 的最小实现 —— 稳定性标签与 C5 稳定性门。

设计 Round 5「将现有 ``replay_lab.label_of`` 接入统一 Aggregator」：
标签逻辑**复用** trust/replay_lab 的 ``label_of``（可信度模型的真值函数，
一行不改），本模块只加「标签 → C5 层结论」的映射，供 ``correctness.judge``
的 stability 参数消费。

标签语义（与 replay_lab 文档一致）：

    stable-green  每遍都成功且分数一致
    drifting      每遍都成功但分数不同（同样的操作，结论不一样）
    flip          有的遍成功有的遍失败 —— 最有价值的一类
    stable-red    每遍都失败
    invalid       有遍因会话/认证失效而「失败」—— 失败与轨迹本身无关，
                  当红标签就是在伪造真值

C5 稳定性门映射：

    stable-green → pass（可复现）
    stable-red   → fail（一致的业务失败，跨 trial 复现）
    flip         → fail（flaky 被检出 —— flaky Case 不允许 pass）
    drifting     → fail（关键轨迹指标漂移 —— 不满足可复现性）
    invalid      → inconclusive（按策略标记；不得错误产生 stable-red，
                   也不得算业务失败）
"""

from __future__ import annotations

from trust.replay_lab import label_of

from trace_eval.correctness import LayerVerdict

VALID_LABELS = ("stable-green", "drifting", "flip", "stable-red", "invalid")


def stability_label(trial_verdicts: list[dict]) -> str:
    """trial 序列 → 稳定性标签。

    ``trial_verdicts``: 每项 ``{"taskSuccess": bool, "score": number,
    "invalid": bool}``（与 replay_lab 的 run 记录同形）。
    """
    if not trial_verdicts:
        raise ValueError("稳定性标签需要至少一个 trial")
    for verdict in trial_verdicts:
        for field in ("taskSuccess", "score", "invalid"):
            if field not in verdict:
                raise ValueError(f"trial 记录缺字段 {field!r}")
    return label_of(list(trial_verdicts))


def stability_layer(case_id: str, label: str,
                    trial_verdicts: list[dict] | None = None) -> LayerVerdict:
    """稳定性标签 → C5 层结论（judge 的 stability 参数）。"""
    if label not in VALID_LABELS:
        raise ValueError(f"未知稳定性标签 {label!r}（可选 {VALID_LABELS}）")
    evidence = [f"trial{i + 1}: "
                + ("invalid" if v.get("invalid")
                   else f"{'pass' if v['taskSuccess'] else 'fail'}"
                   f"(score={v.get('score')})")
                for i, v in enumerate(trial_verdicts or [])]
    if label == "stable-green":
        return LayerVerdict("C5", "stability_gate_passed", "pass", evidence,
                            "每遍一致且分数一致")
    if label == "stable-red":
        return LayerVerdict("C5", "stability_gate_passed", "fail", evidence,
                            "每一遍都失败（一致的业务失败）")
    if label == "flip":
        return LayerVerdict("C5", "stability_gate_passed", "fail", evidence,
                            "flaky：有的遍成功有的遍失败 —— 不允许 pass")
    if label == "drifting":
        return LayerVerdict("C5", "stability_gate_passed", "fail", evidence,
                            "关键轨迹指标漂移（分数不一致）")
    return LayerVerdict("C5", "stability_gate_passed", "inconclusive", evidence,
                        "认证/会话失效的 trial 按策略标记 invalid"
                        "（不是业务失败，也不算红）")
