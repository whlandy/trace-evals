"""打分与 meta-eval 的自检。

分数本身是拍脑袋的（四个罚分只有相对顺序有依据），所以测试守的不是数值，
而是**它必须成立的性质**：坏轨迹罚分更高、干净轨迹不被冤枉、
以及「命中数」不能把没命中的也算进去 —— 最后这条是实测中真的写错过的地方。
"""

import pytest

from test_trust_mutate import _good_trace
from trust.mutate import MUTATIONS, mutate
from trust.score import PENALTY, score_trace
from trust.validate import named_the_failure


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_every_mutation_raises_the_penalty(name):
    """注入缺陷之后罚分必须变高。用罚分不用分数：分数有 0 下限，
    坏透了的轨迹再注入也还是 0 —— 那是量尺到头，不是评估器没抓住。"""
    good = _good_trace()
    before = score_trace(good)["penalty"]
    mutated, described = mutate(good, name)
    after = score_trace(mutated)["penalty"]
    assert after > before, f"{name}（{described}）：罚分 {before} → {after}"


def test_silent_pass_costs_more_than_the_rest():
    """悄悄绿会变成虚假的信心，比失败更伤 —— 失败至少你知道要去查。"""
    assert PENALTY["silent-pass"] > PENALTY["loud-later"] > PENALTY["flaky"] > PENALTY["weak"]


def test_score_declares_that_it_is_not_calibrated():
    """分数只表达排序。说成概率就是撒谎 —— 标签比例根本算不出校准误差。"""
    assert score_trace(_good_trace())["confidence"] == "uncalibrated-ordering-only"


def test_named_the_failure_only_counts_real_hits():
    """实测写错过一次：把所有行都当成命中，5/6 报成了 6/6。"""
    trace = _good_trace()
    labels = {"case-a": {"label": "stable-red",
                         "runs": [{"failedNode": "step_0099"}]}}   # 不存在的节点
    rows = named_the_failure({"case-a": trace}, labels)
    assert rows and rows[0][2] is False


def test_green_traces_are_not_examined_for_failing_nodes():
    """稳定绿的轨迹没有失败节点可言，不该混进这项统计。"""
    labels = {"case-a": {"label": "stable-green", "runs": [{"failedNode": None}]}}
    assert named_the_failure({"case-a": _good_trace()}, labels) == []
