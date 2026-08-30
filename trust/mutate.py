#!/usr/bin/env python3
"""把在册的每一种缺陷注入一条好轨迹 —— 给可信度评估器造负样本。

**为什么必须有这个东西**：真实语料只有 7 条，训不出也验不了任何模型。
但每一种缺陷都真实发生过，而且我们知道它的正确标签 —— 所以可以拿好轨迹
成对地造出 (原体, 变异体)。

它同时是评估器自己的回归测试：变异体的对应特征必须动，将来分数出来之后
还必须**更低**。抓不住某个变异，就说明评估器对那种缺陷是瞎的 ——
而这正是可信度评估器最坏的失败形态：漏看一个缺陷，却报了个好看的数。

每个变异函数就地改一份深拷贝，返回一句人话描述；这条轨迹里没有可下手的
地方就返回 None（不是所有轨迹都有开关、有断言）。

    python3 trust/mutate.py <轨迹目录> ...
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trust._recorder import install_recorder_path              # noqa: E402
install_recorder_path()

import trace_schema as ts                                     # noqa: E402
from trust.features import TEXT_ARGS, trace_features          # noqa: E402


def _prov(node: dict) -> dict:
    return node.setdefault("attach", {}).setdefault("provenance", {})


def _set_sel(node: dict, sel: str) -> None:
    _prov(node).setdefault("selector", {})["sel"] = sel


def blind_toggle(trace: dict) -> str | None:
    """把「拨到指定状态」降级成盲点击 —— 回放时朝哪边拨取决于当时的状态。

    maa-flow5 在 9/17 和 15/17 之间振荡就是这么来的，而且它不报错。
    """
    for node_id, node in ts.nodes(trace):
        action = node.get("action") or {}
        if action.get("type") != "SetSwitch":
            continue
        action["type"] = "Click"
        for key in ("state", "via"):
            (action.get("param") or {}).pop(key, None)
        return f"{node_id}: SetSwitch → Click（丢掉目标状态）"
    return None


def drop_switch_carrier(trace: dict) -> str | None:
    """保留 SetSwitch，但删掉「状态写在哪一层」—— 回放读不出当前状态。"""
    for node_id, node in ts.nodes(trace):
        action = node.get("action") or {}
        if action.get("type") != "SetSwitch" or "via" not in (action.get("param") or {}):
            continue
        action["param"].pop("via")
        return f"{node_id}: 删掉 via（状态层未知）"
    return None


def positional_selector(trace: dict) -> str | None:
    """把作用域选择器换回 CSS 绝对路径 —— 页面多一层浮层，指向就变了。

    实测同一个关闭叉两次录制分别录成 div:nth-of-type(8) 和 (9)。
    """
    for node_id, node in ts.nodes(trace):
        selector = ts.selector_of(node)
        css = selector.get("css") or ""
        if "nth-of-type" not in css or "nth-of-type" in (selector.get("sel") or ""):
            continue
        _set_sel(node, f'locator({json.dumps(css, ensure_ascii=False)})')
        return f"{node_id}: 作用域选择器 → CSS 绝对路径"
    return None


def volatile_anchor(trace: dict) -> str | None:
    """把作用域的锚换成时间戳 —— 这类失败**延迟发作**，录完当场验证不出来。"""
    for node_id, node in ts.nodes(trace):
        sel = ts.selector_of(node).get("sel") or ""
        match = TEXT_ARGS.search(sel)
        if not match:
            continue
        _set_sel(node, sel.replace(match.group(1), "2026-08-14 10:22:29", 1))
        return f"{node_id}: 锚点 {match.group(1)!r} → 时间戳"
    return None


def first_of_many(trace: dict) -> str | None:
    """给选择器加 .first() —— 命中多个时点哪个由页面顺序决定。"""
    for node_id, node in ts.nodes(trace):
        sel = ts.selector_of(node).get("sel") or ""
        if not sel or ".first()" in sel:
            continue
        _set_sel(node, sel + ".first()")
        return f"{node_id}: 追加 .first()"
    return None


def drop_optional(trace: dict) -> str | None:
    """摘掉条件步骤的可选标记 —— 弹窗没出现，整条轨迹就断在那里。"""
    for node_id, node in ts.nodes(trace):
        prov = _prov(node)
        if not prov.get("optional"):
            continue
        prov.pop("optional", None)
        prov.pop("dismissesOverlay", None)
        return f"{node_id}: 摘掉 optional / dismissesOverlay"
    return None


def tautological_assertion(trace: dict) -> str | None:
    """把存在性断言退回同义反复 —— 用文本定位、再断言那段文本，等于没断言。"""
    for node_id, node in ts.nodes(trace):
        spec = ts.assertion_of(node)
        if not spec or spec.get("assertion") != "visible":
            continue
        anchors = [m.group(1) for m in TEXT_ARGS.finditer(
            ts.selector_of(node).get("sel") or "")]
        if not anchors:
            continue
        spec["assertion"] = "text"
        spec["expected"] = anchors[0]
        return f"{node_id}: 存在性断言 → to_have_text({anchors[0]!r})"
    return None


def drop_assertions(trace: dict) -> str | None:
    """删掉所有断言 —— 轨迹只剩「这串操作能走完」。"""
    removed = 0
    for _, node in ts.nodes(trace):
        verification = ts.attach(node).get("verification") or {}
        if verification.pop("assertion", None) is not None:
            removed += 1
    return f"删掉 {removed} 条断言" if removed else None


def drop_expected_body(trace: dict) -> str | None:
    """写请求只剩状态码，不再校验响应体 —— 接口返回什么都算通过。"""
    removed = 0
    for _, node in ts.nodes(trace):
        for expectation in ts.expected_responses(node):
            if expectation.pop("expectedBody", None) is not None:
                removed += 1
    return f"删掉 {removed} 个 expectedBody" if removed else None


def drop_template(trace: dict) -> str | None:
    """删掉视觉模板 —— DOM 一失效就没有回退，且失败原因指不到点上。"""
    for node_id, node in ts.nodes(trace):
        prov = _prov(node)
        if not prov.get("templates"):
            continue
        prov.pop("templates", None)
        prov.pop("templateOrder", None)
        node["recognition"] = {"type": "DirectHit", "param": {}}
        return f"{node_id}: 删掉模板"
    return None


# 变异 → 它必须让哪个汇总特征往哪个方向动。
# 方向由变异的语义决定，与将来的权重无关 —— 这张表是**可证伪**的：
# 变异体的特征没动，就说明抽取器对那种缺陷是瞎的。
MUTATIONS = {
    "blind_toggle":        (blind_toggle, "replay_blindToggles", +1),
    "drop_switch_carrier": (drop_switch_carrier, "replay_switchCarriers", -1),
    "positional_selector": (positional_selector, "replay_positionalSelectors", +1),
    "volatile_anchor":     (volatile_anchor, "replay_volatileAnchors", +1),
    "first_of_many":       (first_of_many, "replay_firstOfMany", +1),
    "drop_optional":       (drop_optional, "replay_optional", -1),
    "tautological_assertion": (tautological_assertion, "evidence_tautologies", +1),
    "drop_assertions":     (drop_assertions, "evidence_assertions", -1),
    "drop_expected_body":  (drop_expected_body, "evidence_responsesWithBody", -1),
    "drop_template":       (drop_template, "observe_templatedSteps", -1),
}


def mutate(trace: dict, name: str) -> tuple[dict, str] | None:
    """返回 (变异后的轨迹, 描述)；这条轨迹没有可下手的地方就返回 None。"""
    mutated = copy.deepcopy(trace)
    described = MUTATIONS[name][0](mutated)
    return (mutated, described) if described else None


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    names = list(MUTATIONS)
    print("%-24s %s" % ("轨迹", "  ".join(n[:9] for n in names)))
    misses = 0
    for arg in argv:
        path = Path(arg)
        path = path / "trace.json" if path.is_dir() else path
        trace = json.loads(path.read_text(encoding="utf-8"))
        base = trace_features(trace)["totals"]
        cells = []
        for name in names:
            result = mutate(trace, name)
            if result is None:
                cells.append("  -  ")          # 这条轨迹没有可下手的地方
                continue
            key, direction = MUTATIONS[name][1], MUTATIONS[name][2]
            after = trace_features(result[0])["totals"][key]
            moved = (after - base[key]) * direction > 0
            cells.append("  ✓  " if moved else "  ✗  ")
            misses += not moved
        print("%-24s %s" % (path.parent.name[:24], " ".join(
            c.center(len(n[:9])) for c, n in zip(cells, names))))
    print(f"\n漏检 {misses} 个（✗ 表示注入了缺陷但特征没动 —— 抽取器对它是瞎的）")
    return 1 if misses else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
