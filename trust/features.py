#!/usr/bin/env python3
"""从 maa 轨迹里抽取可信度特征。

**只抽事实，不打分。** 打分是 P2 的事，而且权重必须由 P1 的实测标签校准 ——
在这里顺手给每条特征配个系数，就等于把拍脑袋的结论固化进数据层，
后面再想校准已经分不清哪些数是量出来的、哪些是猜的。

特征分三组，对应 TRUST-EVAL-PLAN.md 里那三个正交的量：
  replay_*    可回放性：再跑一次还会绿吗
  evidence_*  证据力：它绿了能说明什么
  observe_*   观测充分性：我们凭什么给出上面两个判断

用法：
    python3 trust/features.py <轨迹目录或 trace.json> ...
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# 形状只认 trace_schema 这一处。自己按字段路径去读 attach.provenance.xxx
# 就是在这里再写一份格式定义 —— 轨迹刚从 v1 换到 v2，正好证明了那会漂移。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trust._recorder import install_recorder_path              # noqa: E402
install_recorder_path()
import trace_schema as ts                                    # noqa: E402

# 会变的字面量。锚在它们上面的选择器**延迟发作**：录完当场三遍全绿，
# 几小时后时间推进就再也找不到那一行。录制器里有同一份判断（volatileMarker）。
VOLATILE = re.compile(
    r"\d{4}-\d{2}-\d{2}"                 # 2026-08-14
    r"|\d{1,2}:\d{2}(:\d{2})?"           # 10:22:29
    r"|\d{6,}"                           # 雪花 id / 时间戳
    r"|[0-9a-f]{8}-[0-9a-f]{4}-",        # UUID
    re.I,
)

# 位置依赖的定位方式。页面上多一个同类元素，指向就变了。
POSITIONAL = re.compile(r"nth-of-type\(|\.nth\(")

# 开关组件的通用命名。滑块/轨道/容器都带这些字样。
SWITCHY = re.compile(r"(^|[-_. ])(switch|toggle)([-_. ]|$)", re.I)

# 文本定位器里被当作锚的字符串
TEXT_ARGS = re.compile(r'(?:getByText\(|hasText:\s*)"((?:[^"\\]|\\.)*)"')


def _sel(node: dict) -> str:
    return ts.selector_of(node).get("sel") or ""


def _param(node: dict) -> dict:
    return (node.get("action") or {}).get("param") or {}


def _anchors(sel: str) -> list[str]:
    return [m.group(1) for m in TEXT_ARGS.finditer(sel)]


def node_features(node_id: str, node: dict) -> dict[str, Any]:
    """一个节点的事实。字段名以 replay_/evidence_/observe_ 分组。"""
    action = (node.get("action") or {}).get("type")
    # v2 里断言不产生动作（action 是 DoNothing），规格在 attach.verification
    spec = ts.assertion_of(node)
    sel = _sel(node)
    param = _param(node)
    via = param.get("via") or {}
    responses = ts.expected_responses(node)
    required = [r for r in responses if r.get("required", True)]
    anchors = _anchors(sel)

    features: dict[str, Any] = {
        "nodeId": node_id,
        "action": action,
        "selectorKind": ts.selector_of(node).get("kind"),
        "selectorSel": sel,

        # ── 可回放性 ──
        "replay_positionalSelector": bool(POSITIONAL.search(sel)),
        "replay_firstOfMany": ".first()" in sel,
        "replay_volatileAnchor": [a for a in anchors if VOLATILE.search(a)],
        # 目标看着是开关，动作却是普通点击 —— 回放时朝哪边拨取决于当时的状态，
        # 而且不报错。maa-flow5 在 9/17 和 15/17 之间振荡就是这么来的。
        "replay_blindToggle": bool(action == "Click" and SWITCHY.search(sel)),
        "replay_switchStateCarrier": via.get("type") if action == "SetSwitch" else None,
        "replay_switchGated": bool(via.get("gated")) if action == "SetSwitch" else None,
        "replay_optional": ts.is_optional(node),
        "replay_dismissesOverlay": ts.dismisses_overlay(node),
        # 写请求是这一步真正的副作用，也意味着**下一次回放的起点不同**
        "replay_writeRequests": sum(1 for r in required if r.get("method") != "GET"),
        "replay_runtimeInput": bool(param.get("valueFrom")),

        # ── 证据力 ──
        "evidence_isAssert": bool(spec),
        "evidence_requiredResponses": len(required),
        "evidence_responsesWithBody": sum(
            1 for r in required if r.get("expectedBody") is not None),

        # ── 观测充分性 ──
        "observe_templates": sorted(ts.templates_of(node)),
        "observe_status": ts.node_status(node),
    }

    if spec:
        expected = spec.get("expected")
        features.update({
            "evidence_assertion": spec.get("assertion"),
            "evidence_runtimeExpected": bool(spec.get("expectedFrom")),
            # 用文本定位元素、再断言那段文本，只有元素消失才会失败 —— 等于没断言。
            # 录制器现在会自动改写，但**已经生成的轨迹里仍然有**，所以这里必须认得出来。
            "evidence_tautology": bool(
                spec.get("assertion") == "text"
                and isinstance(expected, str)
                and expected in anchors
            ),
            # 存在性断言是真命题，但它只能证明「这段文字还在」
            "evidence_existenceOnly": (
                spec.get("assertion") == "visible" and spec.get("expected") is True
            ),
        })
    return features


def trace_features(trace: dict) -> dict[str, Any]:
    """整条轨迹的事实：逐节点 + 汇总计数。"""
    nodes = []
    current = ts.meta(trace).get("entry")
    seen = set()
    while current and current not in seen:
        seen.add(current)
        node = trace[current]
        nodes.append(node_features(current, node))
        current = ts.next_of(node)

    asserts = [n for n in nodes if n["evidence_isAssert"]]
    positional_actions = {"Click", "DoubleClick", "Check", "Uncheck", "SetSwitch"}
    needs_template = [n for n in nodes if n["action"] in positional_actions]

    kinds: dict[str, int] = {}
    for n in nodes:
        kinds[n["selectorKind"] or "unknown"] = kinds.get(n["selectorKind"] or "unknown", 0) + 1

    totals = {
        "steps": len(nodes),
        "selectorKinds": kinds,

        "replay_positionalSelectors": sum(n["replay_positionalSelector"] for n in nodes),
        "replay_firstOfMany": sum(n["replay_firstOfMany"] for n in nodes),
        "replay_volatileAnchors": sum(bool(n["replay_volatileAnchor"]) for n in nodes),
        "replay_blindToggles": sum(n["replay_blindToggle"] for n in nodes),
        "replay_switches": sum(n["action"] == "SetSwitch" for n in nodes),
        # 读得出「状态写在哪一层」的开关。读不出就只能盲拨。
        "replay_switchCarriers": sum(bool(n["replay_switchStateCarrier"]) for n in nodes),
        "replay_optional": sum(n["replay_optional"] for n in nodes),
        "replay_dismissesOverlay": sum(n["replay_dismissesOverlay"] for n in nodes),
        # 这条轨迹自己会改动状态 —— 第二次回放的起点和第一次不同
        "replay_writeRequests": sum(n["replay_writeRequests"] for n in nodes),
        "replay_runtimeInputs": sum(n["replay_runtimeInput"] for n in nodes),

        "evidence_assertions": len(asserts),
        "evidence_tautologies": sum(a.get("evidence_tautology", False) for a in asserts),
        "evidence_existenceOnly": sum(a.get("evidence_existenceOnly", False) for a in asserts),
        "evidence_runtimeExpected": sum(a.get("evidence_runtimeExpected", False) for a in asserts),
        "evidence_requiredResponses": sum(n["evidence_requiredResponses"] for n in nodes),
        "evidence_responsesWithBody": sum(n["evidence_responsesWithBody"] for n in nodes),

        "observe_status": ts.meta(trace).get("status"),
        # 计数和比率都要：比率给人看，计数用来比较两条轨迹谁更差
        # （比率在「一个定位步骤都没有」时是 None，没法比方向）
        "observe_templatedSteps": sum(bool(n["observe_templates"]) for n in nodes),
        "observe_templateCoverage": (
            round(sum(bool(n["observe_templates"]) for n in needs_template)
                  / len(needs_template), 4) if needs_template else None
        ),
    }
    return {"name": ts.meta(trace).get("name"), "totals": totals, "nodes": nodes}


def load_trace(path: str | Path) -> tuple[str, dict]:
    path = Path(path)
    if path.is_dir():
        path = path / "trace.json"
    return str(path), json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    report = []
    for arg in argv:
        try:
            path, trace = load_trace(arg)
        except (OSError, ValueError) as error:
            print(f"跳过 {arg}: {error}")
            continue
        item = trace_features(trace)
        item["path"] = path
        item["case"] = Path(path).parent.name
        report.append(item)

    head = ("轨迹", "步", "断言", "同反", "仅存在", "盲开关", "位置选择器",
            "易变锚", ".first", "写请求", "可选", "模板")
    print("%-22s %3s %4s %4s %6s %6s %10s %6s %6s %6s %4s %6s" % head)
    for item in report:
        t = item["totals"]
        print("%-22s %3d %4d %4d %6d %6d %10d %6d %6d %6d %4d %6s" % (
            item["case"][:22], t["steps"], t["evidence_assertions"],
            t["evidence_tautologies"], t["evidence_existenceOnly"],
            t["replay_blindToggles"], t["replay_positionalSelectors"],
            t["replay_volatileAnchors"], t["replay_firstOfMany"],
            t["replay_writeRequests"], t["replay_optional"],
            t["observe_templateCoverage"]))

    out = Path(__file__).resolve().parent / "inventory.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
