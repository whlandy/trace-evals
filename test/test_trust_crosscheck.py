"""对照检查的自检。

关键的是那条**否定**用例：登录前缀是编译时故意切掉的，不能报成掉步。
把它算成缺陷的话，每一条带登录的轨迹都会被冤枉，而真正的掉步就淹没在噪音里。
"""

from trace_v1 import to_v2
from trust.crosscheck import crosscheck


def _rec(*steps):
    return {"steps": list(steps), "net": []}


def _step(sid, **extra):
    return {"id": sid, "type": "click", "sel": f'locator("#{sid}")', **extra}


def _trace_with(*source_ids):
    steps, ids = {}, []
    for i, source in enumerate(source_ids, 1):
        node_id = f"step_{i:04d}"
        ids.append(node_id)
        steps[node_id] = {
            "selector": {"sel": f'locator("#{source}")', "kind": "css"},
            "action": {"type": "Click", "param": {}},
            "status": "ready", "sourceStepId": source,
        }
    for i, node_id in enumerate(ids):
        steps[node_id]["next"] = ids[i + 1] if i + 1 < len(ids) else None
    return to_v2({"schema": "edr.success-trace/v1", "name": "t", "status": "ready",
                  "entry": ids[0] if ids else None, "steps": steps})


def _rules(findings):
    return {f["rule"] for f in findings}


def test_ambiguity_fact_lost_is_reported():
    """录制器验过唯一性、记下了撞几个，轨迹却把这个事实丢了。"""
    findings = crosscheck(
        _rec(_step("a-1", ambiguous=True, matches=2)), _trace_with("a-1"))
    assert "ambiguity_fact_lost" in _rules(findings)


def test_clean_step_produces_nothing():
    assert crosscheck(_rec(_step("a-1")), _trace_with("a-1")) == []


def test_login_prefix_is_not_a_dropped_step():
    """登录步骤是**故意**切掉的。算成掉步的话，每条带登录的轨迹都被冤枉。"""
    recording = _rec(
        _step("a-1", type="fill", secret=True),      # 密码，编译时切掉
        _step("a-2"), _step("a-3"))
    findings = crosscheck(recording, _trace_with("a-2", "a-3"))
    assert "steps_dropped_in_compile" not in _rules(findings)


def test_middle_drop_is_reported():
    """中段掉步是真问题：回放少做一步，却仍按成功计分。"""
    recording = _rec(_step("a-1"), _step("a-2"), _step("a-3"))
    findings = crosscheck(recording, _trace_with("a-1", "a-3"))
    assert "steps_dropped_in_compile" in _rules(findings)


def test_secret_step_reaching_the_trace_is_reported():
    recording = _rec(_step("a-1", type="fill", secret=True))
    findings = crosscheck(recording, _trace_with("a-1"))
    assert "secret_step_in_trace" in _rules(findings)
