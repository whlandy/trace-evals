"""桌面轨迹的规则自检。

这一批规则是被真实语料逼出来的，每条都对应一次**误报**或一个 edr-wd 自己
就标出来的事实。误报在可信度工具里比漏报更伤：报告一旦开始胡说，人就不看了。

实测踩过的三次误报，各有一条测试守着：
  1. 桌面校验放在 verification.verifiers（web 放 assertion）——
     只认一边，9 步带校验的轨迹会被报成「0 条断言」
  2. 滚动步骤的 automationId 在**祖先链**上（目标是那张表），不在控件自己身上
  3. 菜单项没有 automationId 但有 name（「语言设置」）—— Qt 控件本来就是二选一
"""

import json
from pathlib import Path

import pytest

from desktop_to_v2 import DESKTOP_SCHEMA, convert
from trust.features import trace_features
from trust.rules import trace_findings

EDR_WD = Path.home() / "ai-projects" / "edr-wd"


def _desktop(**step):
    base = {"stepId": "step-0001", "actionId": None, "args": {}, "selector": None,
            "endSelector": None, "verifiers": [], "required": True,
            "status": "ready", "issues": [], "next": None}
    return {"schema": DESKTOP_SCHEMA, "status": "ready", "name": "c",
            "sourceRecording": {}, "catalog": {}, "environment": {},
            "entry": "step-0001", "steps": {"step-0001": {**base, **step}}, "cleanup": []}


def _rules(desktop):
    return {f["rule"] for f in trace_findings(convert(desktop))["findings"]}


def _sel(**control):
    return {"control": control, "window": {"processName": "EDRClient.exe"}, "visual": None}


# ── 误报防线 ──

def test_desktop_verifiers_count_as_assertions():
    """桌面校验放在 verifiers 里。只认 web 的 assertion 的话，
    9 步带校验的轨迹会被报成「0 条断言」—— 实测就这么误报过。"""
    trace = convert(_desktop(verifiers=[{"type": "checked", "expected": True}]))
    assert trace_features(trace)["totals"]["evidence_assertions"] == 1
    assert "no_assertions" not in _rules(_desktop(
        verifiers=[{"type": "checked", "expected": True}]))


def test_ancestor_anchored_step_is_not_locatorless():
    """滚动的目标是「那张表」，id 在祖先链上，控件自己没有。"""
    rules = _rules(_desktop(
        actionId="pointer.scroll", args={"clicks": -3},
        selector=_sel(ancestry=[{"automationId": "Win.table", "controlType": "Table"}])))
    assert "no_locator_at_all" not in rules


def test_name_only_control_is_not_locatorless():
    """菜单项没有 automationId 但有 name。Qt 控件本来就是 automation_id 或 text。"""
    rules = _rules(_desktop(actionId="gui.click",
                            selector=_sel(name="语言设置", controlType="MenuItem")))
    assert "no_locator_at_all" not in rules


def test_verification_only_step_is_not_locatorless():
    """只校验不操作的步骤验的是窗口状态，本来就不需要定位控件。"""
    rules = _rules(_desktop(verifiers=[{"type": "window_open", "expected": {"exists": True}}]))
    assert "no_locator_at_all" not in rules


def test_truly_locatorless_step_is_reported():
    """反面：真的什么都没有时必须报 —— 否则上面几条就成了无脑放行。"""
    rules = _rules(_desktop(actionId="gui.click", selector=None))
    assert "no_locator_at_all" in rules


def test_desktop_step_with_selector_is_not_missing_a_template():
    """没有模板在 web 上是缺回退；桌面上有 automationId 就定位得了。"""
    rules = _rules(_desktop(actionId="gui.click", selector=_sel(automationId="Win.btn")))
    assert "no_template" not in rules


# ── 从 edr-wd 自己标的 issues 翻过来的 ──

def test_ambiguous_desktop_selector_is_silent_pass():
    findings = trace_findings(convert(_desktop(
        actionId="gui.click", selector=_sel(automationId="Win.btn"),
        issues=["compile_selector_ambiguous"])))["findings"]
    hit = [f for f in findings if f["rule"] == "ambiguous_desktop_selector"]
    assert hit and hit[0]["failure"] == "silent-pass", findings


def test_action_without_verifier_is_weak_evidence():
    """滚了但没人验证滚到哪 —— 不会失败，也说明不了什么。"""
    findings = trace_findings(convert(_desktop(
        actionId="pointer.scroll", args={"clicks": 3},
        selector=_sel(automationId="Win.table"),
        issues=["compile_scroll_verifier_required"])))["findings"]
    hit = [f for f in findings if f["rule"] == "action_without_verifier"]
    assert hit and hit[0]["failure"] == "weak", findings


def test_unmapped_action_is_reported():
    """没映射的动作编译成 DoNothing —— 回放少做一步却仍报成功。"""
    assert "unmapped_action" in _rules(_desktop(
        actionId="gui.summon_dragon", selector=_sel(automationId="Win.btn")))


def test_non_portable_verification_is_reported():
    """标了 desktop-only 的校验，换个 runtime 跑根本不会发生 ——
    那一步会「通过」，但什么都没验。"""
    assert "verification_not_portable" in _rules(_desktop(
        verifiers=[{"type": "checked", "expected": True}]))


# ── 真实语料 ──

@pytest.mark.skipif(not (EDR_WD / "recordings").is_dir(), reason="本机没有 edr-wd 录制")
def test_real_desktop_recordings_are_auditable():
    """7 条真实桌面录制都体检得了，且没有一条被报成「0 条断言」——
    除非它真的一条校验都没有。"""
    for case in sorted((EDR_WD / "recordings").glob("*/golden-trace.json")):
        source = json.loads(case.read_text(encoding="utf-8"))
        trace = convert(source)
        result = trace_findings(trace)
        assert result["steps"] > 0, case.parent.name
        has_verifier = any(s.get("verifiers") for s in source["steps"].values())
        reported_none = "no_assertions" in {f["rule"] for f in result["findings"]}
        assert not (has_verifier and reported_none), case.parent.name
