"""规则层的自检。

最要紧的一条是 test_every_mutation_produces_a_new_finding：把在册的每一种缺陷
注入一条好轨迹，规则层必须**多出**一条发现项。它把两个模块咬在一起 ——
新加了缺陷却没加规则，或者规则写歪了看不见那个缺陷，这里都会红。

这也是 P2 打分之前唯一能做的单调性检验：分数还没有，但「有缺陷 → 有发现」
必须成立，否则分数建在瞎的基础上。
"""

import pytest

from test_trust_mutate import _good_trace
from trust.mutate import MUTATIONS, mutate
from trust.rules import node_findings, trace_findings
from trust.features import node_features
from trace_v1 import node_to_v2


def _rules_of(trace):
    return {f["rule"] for f in trace_findings(trace)["findings"]}


def _node_rules(flat):
    return {f["rule"] for f in node_findings(node_features("step_0001", node_to_v2(flat)))}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_every_mutation_produces_a_new_finding(name):
    good = _good_trace()
    before = _rules_of(good)
    mutated, described = mutate(good, name)
    after = _rules_of(mutated)
    assert after - before, (
        f"{name}（{described}）注入之后没有新的发现项；"
        f"规则层对这种缺陷是瞎的。现有：{sorted(before)}")


def test_clean_trace_has_no_replay_findings():
    """参考轨迹在可回放性这根轴上应该是干净的 —— 否则上面那条测试会因为
    「本来就有一堆发现」而失去意义。"""
    findings = trace_findings(_good_trace())["findings"]
    replay = [f for f in findings if f["axis"] == "replay"]
    assert [f["rule"] for f in replay] == ["mutates_state"], replay


def test_blind_toggle_is_reported_as_silent():
    """悄悄绿是最坏的一类：事情做错了，回放照样报成功。"""
    findings = node_findings(node_features("step_0001", node_to_v2({
        "selector": {"sel": 'locator(".eui_toggle_thumb")', "kind": "scoped"},
        "action": {"type": "Click", "param": {}}})))
    blind = [f for f in findings if f["rule"] == "blind_toggle"]
    assert blind and blind[0]["failure"] == "silent-pass", findings


def test_volatile_anchor_is_reported_as_delayed():
    """它不是悄悄绿：会真的失败，只是当场验证不出来。"""
    findings = node_findings(node_features("step_0001", node_to_v2({
        "selector": {"sel": 'locator("tr", { hasText: "2026-08-14 10:22:29" })',
                     "kind": "scoped"},
        "action": {"type": "Click", "param": {}}})))
    volatile = [f for f in findings if f["rule"] == "volatile_anchor"]
    assert volatile and volatile[0]["failure"] == "loud-later", findings


def test_existence_only_assertion_is_weak_not_broken():
    """「至少有一个可见」是真命题，不能和同义反复混为一谈。"""
    rules = _node_rules({
        "selector": {"sel": 'getByText("已隔离", { exact: true })', "kind": "text"},
        "action": {"type": "Assert", "param": {"assertion": "visible", "expected": True}}})
    assert "existence_only_assertion" in rules
    assert "tautological_assertion" not in rules


def test_trace_without_assertions_is_flagged():
    trace = _good_trace()
    mutated, _ = mutate(trace, "drop_assertions")
    assert "no_assertions" in _rules_of(mutated)


def test_every_finding_carries_evidence_and_consequence():
    """证据是这一层的主产物。缺了它，发现项就退化成一个没法核实的标签。"""
    for f in trace_findings(_good_trace())["findings"]:
        assert f["evidence"] and f["consequence"], f
        assert f["axis"] in ("replay", "evidence", "observe"), f
        assert f["failure"] in ("silent-pass", "flaky", "loud-later", "weak"), f


def test_environment_data_anchor_is_reported():
    """主机名/IP 是这套环境此刻的库存，不是界面文案。

    P1 的实测标签里 2/6 的失败就断在这上面（getByText("DESKTOP-…-192.0.2.10")）。
    这条规则是被实测标签买来的 —— 不是拍脑袋加的。
    """
    rules = _node_rules({
        "selector": {"sel": 'getByText("DESKTOP-A1B2C3D-192.0.2.10", { exact: true })',
                     "kind": "text"},
        "action": {"type": "Click", "param": {}}})
    assert "environment_data_anchor" in rules


def test_business_label_is_not_a_data_anchor():
    """界面文案不能被误判成环境数据，否则每条轨迹都会被这条规则刷屏。"""
    for text in ("default-group", "开启合规检查策略配置", "应用"):
        rules = _node_rules({
            "selector": {"sel": f'getByText("{text}", {{ exact: true }})', "kind": "text"},
            "action": {"type": "Click", "param": {}}})
        assert "environment_data_anchor" not in rules, text
