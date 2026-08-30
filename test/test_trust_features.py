"""特征抽取的自检。

每条测试守一个**具体的**识别能力：把对应的判据从 features.py 里去掉，
这条就必须变红。只断言「函数能跑通」的测试在这里没有价值 ——
可信度评估器最坏的失败形态就是漏看一个缺陷却报了个好看的数。

节点写平铺形状，由 trace_v1 翻成 v2。不手抄 v2 的嵌套布局：那等于在测试侧
再写一份格式定义，漂移之后测试会拿自己那份形状去验抽取器 —— 轨迹刚从 v1
换到 v2，正好证明了这件事会发生。
"""

from trace_v1 import node_to_v2, to_v2
from trust.features import node_features, trace_features


def _node(flat):
    return node_features("step_0001", node_to_v2(flat))


def _trace(*nodes):
    steps, ids = {}, []
    for i, node in enumerate(nodes, 1):
        node_id = f"step_{i:04d}"
        ids.append(node_id)
        steps[node_id] = {"status": "ready", **node}
    for i, node_id in enumerate(ids):
        steps[node_id]["next"] = ids[i + 1] if i + 1 < len(ids) else None
    return to_v2({"schema": "edr.success-trace/v1", "name": "t", "status": "ready",
                  "entry": ids[0] if ids else None, "steps": steps})


def _click(sel, **extra):
    return {"selector": {"sel": sel, "kind": "scoped"},
            "action": {"type": "Click", "param": {}}, **extra}


def _assert(sel, assertion, expected, **spec):
    return {"selector": {"sel": sel, "kind": "text"},
            "action": {"type": "Assert",
                       "param": {"assertion": assertion, "expected": expected, **spec}}}


# ── 可回放性 ──

def test_blind_toggle_is_detected():
    """目标像开关、动作却是普通点击 —— 回放时朝哪边拨取决于当时状态，且不报错。"""
    node = _node(_click(
        'locator("div.rows", { hasText: "开启合规检查" }).locator(".eui_toggle_thumb")'))
    assert node["replay_blindToggle"] is True


def test_real_switch_step_is_not_blind():
    node = _node({
        "selector": {"sel": 'locator(".eui_toggle_container")', "kind": "scoped"},
        "action": {"type": "SetSwitch",
                   "param": {"state": True, "via": {"type": "class", "token": "toggled"}}},
    })
    assert node["replay_blindToggle"] is False
    assert node["replay_switchStateCarrier"] == "class"


def test_volatile_anchor_is_detected():
    """锚在时间上的作用域延迟发作：录完当场全绿，几小时后再也找不到那一行。"""
    node = _node(_click(
        'locator("tr", { hasText: "2026-08-14 10:22:29" }).locator(".del")'))
    assert node["replay_volatileAnchor"] == ["2026-08-14 10:22:29"]


def test_stable_anchor_is_not_flagged():
    node = _node(_click(
        'locator("tr", { hasText: "default-group" }).locator(".del")'))
    assert node["replay_volatileAnchor"] == []


def test_positional_selector_is_detected():
    node = _node(_click('locator("body > div:nth-of-type(9) > span.close")'))
    assert node["replay_positionalSelector"] is True


def test_write_requests_are_counted_as_state_mutation():
    """写请求意味着**下一次回放的起点不同** —— 这是轨迹自己制造的不确定性。"""
    node = _node({
        **_click('getByRole("button", { name: "应用" })'),
        "expect": {"responses": [
            {"method": "POST", "url": "/api/save", "required": True},
            {"method": "GET", "url": "/api/list", "required": False},
        ]},
    })
    assert node["replay_writeRequests"] == 1


# ── 证据力 ──

def test_tautological_assertion_is_detected():
    """用文本定位元素、再断言那段文本：只有元素消失才会失败，等于没断言。"""
    node = _node(_assert(
        'getByText("WannaCry.exe", { exact: true }).first()', "text", "WannaCry.exe"))
    assert node["evidence_isAssert"] is True
    assert node["evidence_tautology"] is True


def test_assertion_located_otherwise_is_real():
    """testid 找到的元素，断言它的文本是实打实的命题，不能算同义反复。"""
    node = _node({
        "selector": {"sel": 'getByTestId("status")', "kind": "testid"},
        "action": {"type": "Assert",
                   "param": {"assertion": "text", "expected": "已隔离"}}})
    assert node["evidence_tautology"] is False


def test_runtime_expected_assertion_is_marked():
    node = _node(_assert('locator("tr").nth(1)', "text", "2026-08-19 01:29",
                         expectedFrom={"kind": "localtime"}))
    assert node["evidence_runtimeExpected"] is True


# ── 汇总 ──

def test_trace_totals_walk_the_linked_path():
    trace = _trace(
        _click('locator(".eui_toggle_thumb")'),
        _assert('getByText("X")', "text", "X"),
    )
    totals = trace_features(trace)["totals"]
    assert totals["steps"] == 2
    assert totals["replay_blindToggles"] == 1
    assert totals["evidence_assertions"] == 1
    assert totals["evidence_tautologies"] == 1


def test_template_coverage_only_counts_positional_steps():
    """断言步骤不需要模板，不该把覆盖率拉低。"""
    trace = _trace(
        {**_click('locator("#a")'),
         "recognition": {"templates": {"element": "a.png", "context": "b.png"}}},
        _assert('getByText("X")', "visible", True),
    )
    assert trace_features(trace)["totals"]["observe_templateCoverage"] == 1.0
