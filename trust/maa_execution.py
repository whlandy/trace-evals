#!/usr/bin/env python3
"""Deterministically evaluate an edr maa-fw execution trace."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


EXECUTION_SCHEMA = "edr.maa-execution-trace/v1"
EVALUATION_SCHEMA = "edr.maa-trace-evaluation/v1"
META_KEY = "$meta"


class EvaluationError(ValueError):
    """Raised when golden and execution artifacts cannot be evaluated safely."""


def trace_digest(trace: dict) -> str:
    payload = json.dumps(
        trace, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvaluationError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise EvaluationError(f"{path} must contain a JSON object")
    return value


def _golden_contract(golden: dict) -> tuple[dict, list[str]]:
    meta = ((golden.get(META_KEY) or {}).get("attach") or {})
    if not isinstance(meta, dict) or not meta.get("schema"):
        raise EvaluationError("golden trace is missing $meta.attach.schema")
    if meta.get("status") != "ready":
        raise EvaluationError("golden trace is not ready")
    order = meta.get("nodeOrder")
    if not isinstance(order, list) or not all(
        isinstance(item, str) and item for item in order
    ):
        raise EvaluationError("golden $meta.attach.nodeOrder must be a string array")
    if len(order) != len(set(order)):
        raise EvaluationError("golden nodeOrder contains duplicate node IDs")
    missing = [node_id for node_id in order if not isinstance(golden.get(node_id), dict)]
    if missing:
        raise EvaluationError(f"golden nodeOrder references missing nodes: {missing}")
    return meta, order


def _lcs_length(left: list[str], right: list[str]) -> int:
    row = [0] * (len(right) + 1)
    for left_item in left:
        previous = 0
        for index, right_item in enumerate(right, start=1):
            old = row[index]
            if left_item == right_item:
                row[index] = previous + 1
            else:
                row[index] = max(row[index], row[index - 1])
            previous = old
    return row[-1]


def evaluate(golden: dict, execution: dict) -> dict:
    """Evaluate one Maa execution without modifying either input artifact."""
    _meta, expected_order = _golden_contract(golden)
    if execution.get("schema") != EXECUTION_SCHEMA:
        raise EvaluationError(f"unsupported execution schema: {execution.get('schema')!r}")
    execution_golden = execution.get("golden")
    if not isinstance(execution_golden, dict):
        raise EvaluationError("execution.golden must be an object")
    expected_digest = trace_digest(golden)
    if execution_golden.get("digest") != expected_digest:
        raise EvaluationError("execution was produced from a different golden trace")
    if execution_golden.get("nodeOrder") != expected_order:
        raise EvaluationError("execution golden nodeOrder differs from the supplied golden trace")

    steps = execution.get("steps")
    if not isinstance(steps, list):
        raise EvaluationError("execution.steps must be an array")
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or not isinstance(step.get("nodeId"), str):
            raise EvaluationError(f"execution.steps.{index} is missing nodeId")
        retries = step.get("retries", 0)
        if type(retries) is not int or retries < 0:
            raise EvaluationError(
                f"execution.steps.{index}.retries must be a non-negative integer"
            )

    observed_order = [step["nodeId"] for step in steps]
    step_by_id: dict[str, dict] = {}
    duplicate_ids: list[str] = []
    for step in steps:
        node_id = step["nodeId"]
        if node_id in step_by_id:
            duplicate_ids.append(node_id)
        else:
            step_by_id[node_id] = step

    def satisfied(node_id: str) -> bool:
        step = step_by_id.get(node_id, {})
        if step.get("status") == "success":
            return True
        provenance = ((golden[node_id].get("attach") or {}).get("provenance") or {})
        return step.get("status") == "skipped" and bool(provenance.get("optional"))

    completed = sum(satisfied(node_id) for node_id in expected_order)
    action_hits = 0
    for node_id in expected_order:
        step = step_by_id.get(node_id) or {}
        expected_action = (golden[node_id].get("action") or {}).get("type")
        if step.get("actualAction") == expected_action:
            action_hits += 1

    total = len(expected_order)
    lcs = _lcs_length(expected_order, observed_order)
    order_rate = lcs / max(total, len(observed_order)) if total or observed_order else 1.0
    extra_actions = max(0, len(observed_order) - lcs)
    retries = sum(step.get("retries", 0) for step in steps)
    completion_rate = completed / total if total else float(not steps)
    action_accuracy = action_hits / total if total else float(not steps)
    efficiency = total / (total + extra_actions + retries) if total else float(not steps)
    visual_scores = [
        float(step["matchScore"])
        for step in steps
        if isinstance(step.get("matchScore"), (int, float))
        and not isinstance(step.get("matchScore"), bool)
    ]
    visual_mean = sum(visual_scores) / len(visual_scores) if visual_scores else None
    confidence_component = visual_mean if visual_mean is not None else 1.0
    failed_nodes = [node_id for node_id in expected_order if not satisfied(node_id)]
    exact_path = observed_order == expected_order and not duplicate_ids
    task_success = (
        execution.get("status") == "success"
        and completed == total
        and exact_path
        and not failed_nodes
    )
    score = 100 * (
        0.50 * float(task_success)
        + 0.20 * completion_rate
        + 0.10 * action_accuracy
        + 0.10 * order_rate
        + 0.05 * efficiency
        + 0.05 * max(0.0, min(1.0, confidence_component))
    )
    return {
        "schema": EVALUATION_SCHEMA,
        "taskSuccess": task_success,
        "score": round(score, 2),
        "stepCompletionRate": round(completion_rate, 4),
        "actionAccuracy": round(action_accuracy, 4),
        "trajectoryOrderRate": round(order_rate, 4),
        "trajectoryEfficiency": round(efficiency, 4),
        "averageVisualMatchScore": round(visual_mean, 4) if visual_mean is not None else None,
        "expectedStepCount": total,
        "observedStepCount": len(steps),
        "extraActionCount": extra_actions,
        "retryCount": retries,
        "failedNodeIds": failed_nodes,
        "duplicateNodeIds": sorted(set(duplicate_ids)),
        "traceIntegrity": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", required=True, type=Path)
    parser.add_argument("--execution", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = evaluate(_load(args.golden), _load(args.execution))
    except EvaluationError as error:
        parser.exit(2, f"evaluation error: {error}\n")
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if report["taskSuccess"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
