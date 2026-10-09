#!/usr/bin/env python3
"""把 edr.execution-trace/v1 转成 oracle 使用的运行时 observations。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trust.trajectory_match import canonical_steps

EXECUTION_SCHEMA = "edr.execution-trace/v1"


def _point_in_box(point, box) -> bool | None:
    if not (isinstance(point, dict) and isinstance(box, dict)):
        return None
    try:
        x, y = float(point["x"]), float(point["y"])
        left, top = float(box["x"]), float(box["y"])
        width, height = float(box["width"]), float(box["height"])
    except (KeyError, TypeError, ValueError):
        return None
    return width > 0 and height > 0 and left <= x <= left + width and top <= y <= top + height


def _normalized_edge_margin(point, box) -> float | None:
    """点击点到最近 bbox 边缘的距离，以对应宽/高归一化；框外为负。"""
    if not (isinstance(point, dict) and isinstance(box, dict)):
        return None
    try:
        x, y = float(point["x"]), float(point["y"])
        left, top = float(box["x"]), float(box["y"])
        width, height = float(box["width"]), float(box["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    horizontal = min(x - left, left + width - x) / width
    vertical = min(y - top, top + height - y) / height
    return round(min(horizontal, vertical), 4)


def observations_from_execution(trace: dict, execution: dict) -> dict[str, dict]:
    if execution.get("schema") != EXECUTION_SCHEMA:
        raise ValueError(f"不支持的 execution schema：{execution.get('schema')!r}")
    expected = {step["nodeId"]: step for step in canonical_steps(trace)}
    observations = {}
    execution_by_id = {item.get("nodeId"): item for item in execution.get("steps") or []}
    for item in execution.get("steps") or []:
        node_id = item.get("nodeId")
        if node_id not in expected:
            continue
        status = item.get("status")
        target = item.get("target")
        target_observation = {}
        if isinstance(target, dict):
            target_observation["resolved"] = status == "success"
            target_observation["resolutionEvidenceSource"] = "edr-target-resolver"
            target_observation["resolutionEvidence"] = [
                f"target-mode={target.get('mode')}", f"execution-status={status}"]
            target_observation["mode"] = target.get("mode")
            for key in ("matchScore", "verifyScore", "scale", "point", "boundingBox",
                        "matchCount", "visible", "enabled", "unobscured",
                        "semanticMatch", "semanticIdentity", "semanticEvidence",
                        "semanticEvidenceSource", "actionabilityEvidence",
                        "actionabilityEvidenceSource", "domError"):
                if key in target:
                    target_observation[key] = target[key]
            if "matchCount" in target:
                target_observation["matchEvidenceSource"] = "edr-target-resolver"
                target_observation["matchEvidence"] = [
                    f"runtime-match-count={target.get('matchCount')}"]
            if (status == "success" and expected[node_id].get("action") in
                    {"Click", "DoubleClick", "SetSwitch", "Check", "Uncheck"}
                    and target.get("mode") in {"dom", "visual"}):
                # DOM: Playwright actionability + click 成功；visual: click point 由匹配框计算。
                # 这只证明点落在运行时选中的目标，不证明选中的业务对象语义正确。
                geometric_hit = _point_in_box(target.get("point"), target.get("boundingBox"))
                hit_margin = _normalized_edge_margin(target.get("point"),
                                                     target.get("boundingBox"))
                target_observation["hitWithinTarget"] = (
                    geometric_hit if geometric_hit is not None else True)
                target_observation["hitEvidence"] = (
                    ("point-in-runtime-bounding-box" if geometric_hit else
                     "point-outside-runtime-bounding-box") if geometric_hit is not None else
                    "playwright-actionability" if target.get("mode") == "dom" else
                    "visual-match-derived-point")
                target_observation["hitInference"] = (
                    "measured" if geometric_hit is not None else "executor-derived")
                if hit_margin is not None:
                    target_observation["hitMarginNormalized"] = hit_margin
                # locator.click/check 等在非 force 模式下只有通过 Playwright actionability
                # 才会成功。只为执行记录未显式给出的字段补证据；显式 false 不覆盖。
                forced = bool((expected[node_id].get("arguments") or {}).get("force"))
                if target.get("mode") == "dom" and not forced:
                    inferred = {}
                    for field, proof in (("visible", "playwright:displayed"),
                                         ("enabled", "playwright:enabled"),
                                         ("unobscured", "playwright:receives-pointer-events")):
                        if field not in target_observation:
                            target_observation[field] = True
                            inferred[field] = [proof]
                    if inferred:
                        target_observation.setdefault(
                            "actionabilityEvidenceSource", "playwright-actionability")
                        evidence_map = target_observation.setdefault("actionabilityEvidence", {})
                        if isinstance(evidence_map, dict):
                            for field, proof in inferred.items():
                                evidence_map.setdefault(field, proof)
        elif status in ("failed", "skipped"):
            target_observation["resolved"] = False
            target_observation["resolutionEvidenceSource"] = "edr-target-resolver"
            target_observation["resolutionEvidence"] = [f"execution-status={status}"]

        # replay_trace 在动作前建立 expect_response waiter，退出动作上下文后才读取响应。
        # 因此 execution step 内的响应具备 action-scoped temporal attribution。
        reactions = {"network": [
            {**response, "causallyLinked": True,
             "causalEvidence": "action-scoped-response-waiter",
             "causalEvidenceSource": "action-scoped-response-waiter",
             "validationEvidenceSource": "edr-response-validator",
             "validationEvidence": [f"replay-response-ok={bool(response.get('ok'))}"]}
            for response in (item.get("responses") or [])
        ]}
        if expected[node_id].get("assertion"):
            reactions["assertions"] = [{
                "passed": status == "success",
                "error": item.get("error") if status != "success" else None,
                "evidenceSource": "playwright-assertion",
                "evidence": [f"execution-status={status}"],
            }]
        observations[node_id] = {
            "target": target_observation,
            "action": {
                "type": item.get("actualAction"),
                "completed": status == "success",
                "skipped": status == "skipped",
                "status": status,
                "error": item.get("error"),
                "retries": item.get("retries", 0),
                "durationMs": item.get("durationMs"),
                "evidenceSource": "edr.execution-trace/v1",
                "evidence": [f"actualAction={item.get('actualAction')}", f"status={status}"],
            },
            "reactions": reactions,
            "evidenceSource": "edr.execution-trace/v1",
        }
    ordered = canonical_steps(trace)
    for current, following in zip(ordered, ordered[1:]):
        current_observation = observations.get(current["nodeId"])
        if current_observation is None:
            continue
        next_execution = execution_by_id.get(following["nodeId"])
        current_observation["reactions"]["nextStep"] = (
            {"nodeId": following["nodeId"], "status": next_execution.get("status"),
             "ready": next_execution.get("status") in {"success", "skipped"},
             "evidenceSource": "edr-execution-sequence",
             "evidence": [f"next-execution-status={next_execution.get('status')}"]}
            if next_execution else None)
        if following.get("assertion"):
            following_observation = observations.get(following["nodeId"])
            assertion_rows = ((following_observation or {}).get("reactions") or {}).get(
                "assertions")
            current_observation["reactions"]["downstreamAssertion"] = (
                {"nodeId": following["nodeId"], **assertion_rows[0]}
                if assertion_rows else None)
    return observations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace")
    parser.add_argument("execution")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    def load(path):
        value = Path(path)
        value = value / "trace.json" if value.is_dir() else value
        return json.loads(value.read_text(encoding="utf-8"))
    result = observations_from_execution(load(args.trace), load(args.execution))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
