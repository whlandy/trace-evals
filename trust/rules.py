#!/usr/bin/env python3
"""把特征翻成**带证据的发现项**。

这一层仍然不打分。权重要等 P1 的实测标签来校准，在这里顺手给每条规则配个
「严重度」，就是把拍脑袋的结论伪装成事实。

取而代之的是一个真正的事实维度：**这个缺陷失败时是什么形态**。
它比严重度更能决定你该多担心：

    silent-pass   悄悄绿 —— 事情做错了，回放照样报成功。最坏的一类。
    flaky         时好时坏 —— 换个起点结果就变，同一条轨迹两次不同分。
    loud-later    当时不报，以后才炸 —— 录完当场三遍全绿，隔几小时就红。
    weak          不会失败，但它绿了也说明不了什么。

每条发现项都带 evidence（从轨迹里读到的原话）和 consequence（会发生什么）。
**分数会被优化，证据不会** —— 所以证据是这一层的主产物，计数只是摘要。

    python3 trust/rules.py <轨迹目录> ...
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trust._recorder import install_recorder_path              # noqa: E402
install_recorder_path()

from trust.features import trace_features                     # noqa: E402

import re

# 关闭件的通用命名。录制器判「这一下是不是在关浮层」时用的是同一族判据。
CLOSER_SEL = re.compile(r'\.locator\("[^"]*(close|dismiss)', re.I)

# 环境数据：主机名、IP、资产名。它们不是界面文案，是**这套环境此刻的库存**。
# 和时间戳同类，只是变得慢一些 —— 换台机器、资产下线、换个筛选条件，
# 这一行就不在了。P1 的实测标签里有 2/6 的失败就断在这上面。
DATA_ANCHOR = re.compile(
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b"          # 192.0.2.10
    r"|\b[A-Z0-9]{4,}-[A-Z0-9-]{4,}\b",        # DESKTOP-A1B2C3D-…
)

SILENT_PASS = "silent-pass"
FLAKY = "flaky"
LOUD_LATER = "loud-later"
WEAK = "weak"

REPLAY, EVIDENCE, OBSERVE = "replay", "evidence", "observe"


def _anchors_of(node: dict) -> list[str]:
    from trust.features import TEXT_ARGS
    return [m.group(1) for m in TEXT_ARGS.finditer(node.get("selectorSel") or "")]


def _finding(rule, axis, failure, node, evidence, consequence):
    return {"rule": rule, "axis": axis, "failure": failure, "node": node,
            "evidence": evidence, "consequence": consequence}


def node_findings(node: dict) -> list[dict]:
    """逐节点的发现项。node 是 features.node_features 的产物。"""
    out = []
    node_id = node["nodeId"]

    if node["replay_blindToggle"]:
        out.append(_finding(
            "blind_toggle", REPLAY, SILENT_PASS, node_id,
            "动作是 Click，而目标看着是开关组件",
            "回放时朝哪个方向拨取决于当时的状态，拨反了也不报错"))

    if node["action"] == "SetSwitch" and not node["replay_switchStateCarrier"]:
        out.append(_finding(
            "switch_without_carrier", REPLAY, SILENT_PASS, node_id,
            "SetSwitch 没有 via（状态写在哪一层未知）",
            "读不出当前状态，只能盲拨，等同于普通点击"))

    if node["replay_volatileAnchor"]:
        out.append(_finding(
            "volatile_anchor", REPLAY, LOUD_LATER, node_id,
            f"作用域锚在会变的值上：{node['replay_volatileAnchor']}",
            "录完当场全绿；时间推进后再也找不到那一行，且报错指不到原因"))

    anchors = [a for a in _anchors_of(node) if DATA_ANCHOR.search(a)]
    if anchors:
        out.append(_finding(
            "environment_data_anchor", REPLAY, LOUD_LATER, node_id,
            f"选择器锚在环境数据上：{anchors}",
            "主机名/IP 是这套环境此刻的库存，不是界面文案 —— "
            "资产下线或换个筛选条件，这一行就不在了"))

    if node["replay_positionalSelector"]:
        out.append(_finding(
            "positional_selector", REPLAY, LOUD_LATER, node_id,
            "选择器按位置定位（nth-of-type / nth）",
            "页面多一层浮层或多一行，指向就变了"))

    if node["replay_firstOfMany"]:
        out.append(_finding(
            "first_of_many", REPLAY, SILENT_PASS, node_id,
            "选择器以 .first() 收尾",
            "命中多个时点/断言哪一个由页面顺序决定，选错了也不报错"))

    if node["replay_writeRequests"]:
        out.append(_finding(
            "mutates_state", REPLAY, FLAKY, node_id,
            f"{node['replay_writeRequests']} 个必发的写请求",
            "这一步改动了下一次回放的起点 —— 第二遍不是重复实验"))

    # 标记被摘掉之后，轨迹从表面上看是干净的 —— 缺陷在产物里**不可见**。
    # 能看见的间接症状只有这个：看着像关闭图标的点击，却是必经节点。
    # 判据是命名习惯，会漏（带文字的「我知道了」按钮就漏），但不会乱指：
    # 真正的业务图标不会叫 close。
    if (CLOSER_SEL.search(node.get("selectorSel") or "")
            and not node["replay_optional"]):
        out.append(_finding(
            "unmarked_dismissal", REPLAY, LOUD_LATER, node_id,
            "看着是关闭图标的点击，却是必经节点",
            "弹窗出现与否取决于账号状态和历史操作；它不出现时整条轨迹断在这一步"))

    if node["evidence_isAssert"]:
        if node.get("evidence_tautology"):
            out.append(_finding(
                "tautological_assertion", EVIDENCE, SILENT_PASS, node_id,
                "用文本定位元素，再断言那段文本",
                "只有元素消失才会失败 —— 它绿了什么都不能说明"))
        elif node.get("evidence_existenceOnly"):
            out.append(_finding(
                "existence_only_assertion", EVIDENCE, WEAK, node_id,
                "断言只是「至少有一个可见」",
                "是真命题，但只能证明这段文字还在，证不了系统做对了什么"))

    # ── 桌面侧 ──
    # 判据来自 edr-wd 编译期自己标的 issues。它已经知道的事，我们不再推一遍，
    # 只把它翻成同一套失败形态 —— 这样两个 runtime 的问题能放在一张表里比。
    if node["desktop_unmappedAction"]:
        out.append(_finding(
            "unmapped_action", REPLAY, SILENT_PASS, node_id,
            f"动作 {node['desktop_unmappedAction']!r} 没有映射，编译成了 DoNothing",
            "回放会跳过这一步却仍按成功计分 —— 少做一步也报绿"))

    for issue in node["desktop_issues"]:
        if issue == "compile_selector_ambiguous":
            out.append(_finding(
                "ambiguous_desktop_selector", REPLAY, SILENT_PASS, node_id,
                "录制器标了 compile_selector_ambiguous（控件不唯一）",
                "回放可能点到另一个长得一样的控件，而且不报错"))
        elif issue.endswith("_verifier_required"):
            out.append(_finding(
                "action_without_verifier", EVIDENCE, WEAK, node_id,
                f"录制器标了 {issue}（这个动作没人校验它落在哪）",
                "滚动/拖拽做完了没有任何检查，这一步绿了说明不了什么"))
        else:
            out.append(_finding(
                "compiler_flagged_issue", OBSERVE, LOUD_LATER, node_id,
                f"录制器标了 {issue}",
                "编译期就知道这一步有问题，回放前该先看它"))

    # 只校验、不操作的步骤（actionId 为空）本来就不需要定位控件 —— 它验的是
    # 窗口状态。对它报「没有定位依据」是误报。
    needs_target = node["action"] not in ("DoNothing",)
    if node["app"] == "desktop" and needs_target \
            and not node["desktop_hasSelector"] and not node["observe_templates"]:
        out.append(_finding(
            "no_locator_at_all", REPLAY, SILENT_PASS, node_id,
            "既没有控件选择器，也没有视觉模板",
            "回放没有任何依据定位目标，点了等于随机点 —— 而且不报错"))

    if node["evidence_isAssert"] and node["evidence_verifyScope"]:
        out.append(_finding(
            "verification_not_portable", EVIDENCE, WEAK, node_id,
            f"这条校验标了 {node['evidence_verifyScope']}",
            "换一个 runtime 跑，这条检查根本不会发生 —— 那一步会「通过」，"
            "但什么都没验"))

    if node["evidence_requiredResponses"] and not node["evidence_responsesWithBody"]:
        out.append(_finding(
            "response_without_body", EVIDENCE, WEAK, node_id,
            f"{node['evidence_requiredResponses']} 个必发请求只校验状态码",
            "接口返回 200 但内容是错的，这一步照样通过"))

    # 没有模板在 web 上是缺回退；桌面上只要有 automationId 就定位得了，
    # 那种情形归 no_locator_at_all 管，不该在这里重复报一次。
    if node["action"] in ("Click", "DoubleClick", "SetSwitch", "Check", "Uncheck") \
            and not node["observe_templates"] \
            and not (node["app"] == "desktop" and node["desktop_hasSelector"]):
        out.append(_finding(
            "no_template", OBSERVE, LOUD_LATER, node_id,
            "定位类步骤没有视觉模板",
            "DOM 选择器一失效就没有回退，且失败原因指不到点上"))

    return out


def trace_findings(trace: dict) -> dict:
    """整条轨迹的发现项 + 摘要计数。仍然不给分。"""
    extracted = trace_features(trace)
    nodes = extracted["nodes"]
    totals = extracted["totals"]

    findings = [f for node in nodes for f in node_findings(node)]

    # ── 只有站在整条轨迹的高度才看得见的两条 ──

    if totals["evidence_assertions"] == 0:
        findings.append(_finding(
            "no_assertions", EVIDENCE, SILENT_PASS, None,
            f"{totals['steps']} 步，0 条断言",
            "这条轨迹只证明「这串操作能走完」，不证明系统做对了任何事"))

    # 关浮层的证据是**录制时观察出来的**。整条轨迹一个标记都没有，却又带着
    # CSS 路径步骤（那多半就是关弹窗）—— 说明它录制时还没有那个观察器。
    # 这类缺失重新生成补不回来：观察器没跑过，事实就不存在。
    if totals["replay_dismissesOverlay"] == 0 and totals["replay_positionalSelectors"]:
        findings.append(_finding(
            "recorder_predates_overlay_evidence", OBSERVE, LOUD_LATER, None,
            f"0 个关浮层标记，却有 {totals['replay_positionalSelectors']} 个 CSS 路径步骤",
            "条件步骤全被当成必经节点；这类轨迹只能重录，重新生成补不回来"))

    by_failure: dict[str, int] = {}
    by_axis: dict[str, int] = {}
    for f in findings:
        by_failure[f["failure"]] = by_failure.get(f["failure"], 0) + 1
        by_axis[f["axis"]] = by_axis.get(f["axis"], 0) + 1

    return {"name": extracted["name"], "steps": totals["steps"],
            "findings": findings, "byFailure": by_failure, "byAxis": by_axis}


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    report = []
    for arg in argv:
        path = Path(arg)
        path = path / "trace.json" if path.is_dir() else path
        result = trace_findings(json.loads(path.read_text(encoding="utf-8")))
        result["case"] = path.parent.name
        report.append(result)

    order = [SILENT_PASS, FLAKY, LOUD_LATER, WEAK]
    print("%-24s %4s %6s  %s" % ("轨迹", "步", "发现", "  ".join(
        f"{k:>11}" for k in order)))
    for item in report:
        print("%-24s %4d %6d  %s" % (
            item["case"][:24], item["steps"], len(item["findings"]),
            "  ".join(f"{item['byFailure'].get(k, 0):>11}" for k in order)))

    print("\n—— 逐条证据（silent-pass 优先，它是最坏的一类）——")
    for item in report:
        worst = [f for f in item["findings"] if f["failure"] == SILENT_PASS]
        if not worst:
            continue
        print(f"\n{item['case']}:")
        for f in worst[:4]:
            where = f["node"] or "整条轨迹"
            print(f"  [{f['rule']}] {where}: {f['evidence']}")
            print(f"      → {f['consequence']}")

    out = Path(__file__).resolve().parent / "findings.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
