"""变异器的自检。

语料在仓库外（7 条真实轨迹），测试不能依赖它 —— 所以这里自造一条**样样俱全**
的好轨迹：有开关、有作用域选择器、有条件步骤、有断言、有写请求、有模板。

两条硬性质：

  1. 每一种在册变异都能在这条轨迹上下手。新加变异却没扩充这条夹具的话，
     这里会立刻喊出来 —— 否则那个变异会长期处于「没被验过」的状态。
  2. 变异之后，它声明要影响的那个特征必须朝声明的方向动。动不了就说明
     抽取器对那种缺陷是瞎的，而这正是可信度评估器最坏的失败形态。
"""

import copy

import pytest

from trace_v1 import to_v2
from trust.features import trace_features
from trust.mutate import MUTATIONS, mutate


def _good_trace():
    """一条样样俱全的好轨迹。每个节点各自带一种可被注入缺陷的特征。"""
    steps = {
        "step_0001": {                                   # 条件步骤：关浮层
            "selector": {
                "sel": 'locator("div.dlg", { hasText: "卸载校验码" }).locator(".close")',
                "kind": "scoped",
                "css": "body > div:nth-of-type(9) > span.close",
            },
            "action": {"type": "Click", "param": {}},
            "recognition": {"templates": {"element": "a.png", "context": "b.png"}},
            "optional": True, "dismissesOverlay": True,
            "next": "step_0002",
        },
        "step_0002": {                                   # 开关：状态层可读
            "selector": {"sel": 'locator(".eui_toggle_container")', "kind": "scoped"},
            "action": {"type": "SetSwitch",
                       "param": {"state": True,
                                 "via": {"type": "class", "token": "toggled"}}},
            "recognition": {"templates": {"element": "c.png"}},
            "next": "step_0003",
        },
        "step_0003": {                                   # 写请求 + 响应体
            "selector": {"sel": 'getByRole("button", { name: "应用" })', "kind": "role"},
            "action": {"type": "Click", "param": {}},
            "recognition": {"templates": {"element": "d.png"}},
            "expect": {"responses": [{
                "method": "POST", "url": "/api/save", "required": True,
                "expectedStatus": 200, "expectedBody": {"code": "200"},
            }]},
            "next": "step_0004",
        },
        "step_0004": {                                   # 存在性断言
            "selector": {"sel": 'getByText("已隔离", { exact: true })', "kind": "text"},
            "action": {"type": "Assert",
                       "param": {"assertion": "visible", "expected": True}},
            "next": None,
        },
    }
    for node_id, node in steps.items():
        node.setdefault("status", "ready")
    return to_v2({"schema": "edr.success-trace/v1", "name": "good", "status": "ready",
                  "entry": "step_0001", "steps": steps})


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_every_mutation_applies_to_the_reference_trace(name):
    """新加变异却没扩充夹具的话，那个变异会长期没被验过。"""
    assert mutate(_good_trace(), name) is not None, f"{name} 在参考轨迹上没有可下手的地方"


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_every_mutation_moves_its_declared_feature(name):
    trace = _good_trace()
    before = trace_features(trace)["totals"]
    mutated, described = mutate(trace, name)
    after = trace_features(mutated)["totals"]

    _, key, direction = MUTATIONS[name]
    delta = after[key] - before[key]
    assert delta * direction > 0, (
        f"{name}（{described}）：{key} {before[key]} → {after[key]}，"
        f"期望方向 {direction:+d}")


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_mutation_does_not_touch_the_original(name):
    """就地改原体的话，会把语料和后续的对照全部污染。"""
    trace = _good_trace()
    snapshot = copy.deepcopy(trace)
    mutate(trace, name)
    assert trace == snapshot, f"{name} 改到了原体上"


def test_mutations_are_not_silently_no_ops_on_a_bare_trace():
    """没有可下手的地方就该返回 None，而不是假装做了。"""
    bare = to_v2({"schema": "edr.success-trace/v1", "name": "bare", "status": "ready",
                  "entry": "step_0001", "steps": {"step_0001": {
                      "selector": {"sel": 'locator("#x")', "kind": "css"},
                      "action": {"type": "Click", "param": {}},
                      "status": "ready", "next": None}}})
    assert mutate(bare, "blind_toggle") is None
    assert mutate(bare, "drop_assertions") is None
    assert mutate(bare, "drop_template") is None
