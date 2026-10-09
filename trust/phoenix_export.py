#!/usr/bin/env python3
"""把静态 trace 与 trust 评分导出为 Phoenix spans/annotations。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from trust.step_score import score_steps
from trust.trajectory_match import canonical_steps

ANNOTATION_IDENTIFIER = "trace-eval-v1"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _annotation(name: str, score: float | None = None, *, label: str | None = None,
                explanation: str = "", source: str = "rules",
                identifier: str = ANNOTATION_IDENTIFIER) -> dict:
    kinds = {"rules": "CODE", "hybrid": "CODE", "model": "LLM", "human": "HUMAN"}
    if source not in kinds:
        raise ValueError(f"未知 Phoenix annotation source：{source}")
    return {
        "name": name,
        "annotator_kind": kinds[source],
        "result": {**({"score": score} if score is not None else {}),
                   **({"label": label} if label else {}),
                   **({"explanation": explanation} if explanation else {})},
        "identifier": identifier,
        "metadata": {"source": source,
                     "interpretation": "ordering-only-not-a-probability"},
    }


def build_phoenix_plan(trace: dict, evaluation: dict | None = None) -> dict:
    """纯函数：先生成可检查的 export plan，再由网络层发送。"""
    evaluation = evaluation or score_steps(trace)
    rubric_version = (evaluation.get("provenance") or {}).get("rubricVersion") or "rules-v1"
    def annotation_id(source: str) -> str:
        return f"trace-eval:{rubric_version}:{source}"
    semantic_steps = canonical_steps(trace)
    evaluations = {step["nodeId"]: step for step in evaluation["steps"]}
    oracle_by_node = {
        step.get("actualNodeId") or step.get("referenceNodeId"): step
        for step in (evaluation.get("oracle") or {}).get("steps", [])
    }
    children = []
    for semantic in semantic_steps:
        node_id = semantic["nodeId"]
        scored = evaluations[node_id]
        annotations = []
        for dimension, value in scored["scores"].items():
            dimension_evidence = (scored.get("scoreEvidence") or {}).get(dimension, {})
            explanation = dimension_evidence.get("reason", "")
            evidence = dimension_evidence.get("evidence") or []
            if evidence:
                explanation = f"{explanation} | evidence: {', '.join(evidence)}"
            annotations.append(_annotation(f"trace_eval.{dimension}", value,
                                           explanation=explanation,
                                           source=scored.get("source", "rules"),
                                           identifier=annotation_id(scored.get("source", "rules"))))
        annotations.append(_annotation(
            "trace_eval.step_overall", scored["overall"], label=scored["label"],
            explanation=scored.get("reason", ""), source=scored.get("source", "rules"),
            identifier=annotation_id(scored.get("source", "rules"))))
        for claim_name, claim in (scored.get("claims") or {}).items():
            annotations.append(_annotation(
                f"trace_eval.claim.{claim_name}", label=claim["verdict"],
                explanation=claim["reason"], source="model",
                identifier=annotation_id("model")))
        for oracle_check in oracle_by_node.get(node_id, {}).get("checks", []):
            annotations.append(_annotation(
                f"trace_eval.oracle.{oracle_check['criterion']}",
                score=oracle_check["score"], label=oracle_check["verdict"],
                explanation=oracle_check["reason"], source="rules",
                identifier=annotation_id("rules")))
        children.append({
            "key": node_id,
            "name": f"trace.step.{node_id}.{semantic.get('action') or 'unknown'}",
            "attributes": {
                "openinference.span.kind": "TOOL",
                "trace_eval.node_id": node_id,
                "trace_eval.action": semantic.get("action") or "unknown",
                "input.value": _json({"arguments": semantic["arguments"],
                                      "selector": semantic["selector"]}),
                "output.value": _json({"assertion": semantic["assertion"],
                                       "responses": semantic["responses"]}),
                "trace_eval.findings": _json(scored.get("findings", [])),
                "trace_eval.confidence": scored["confidence"],
            },
            "annotations": annotations,
        })

    aggregate = evaluation.get("aggregate") or {}
    root_annotations = []
    trajectory = evaluation.get("trajectory") or {}
    trajectory_evidence = trajectory.get("trajectoryEvidence") or {}
    for key in ("completeness", "necessity", "ordering", "evidence_closure"):
        value = trajectory.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        evidence_item = trajectory_evidence.get(key) or {}
        explanation = evidence_item.get("reason", "")
        evidence = evidence_item.get("evidence") or []
        if evidence:
            explanation = f"{explanation} | evidence: {', '.join(evidence)}"
        root_annotations.append(_annotation(f"trace_eval.trajectory.{key}", float(value),
                                            explanation=explanation, source="model",
                                            identifier=annotation_id("model")))
    for key, value in aggregate.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            source = "rules" if "deterministic" in key.lower() else "hybrid"
            root_annotations.append(_annotation(f"trace_eval.aggregate.{key}", float(value),
                                                source=source,
                                                identifier=annotation_id(source)))
    for key, value in ((evaluation.get("oracle") or {}).get("summary") or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            root_annotations.append(_annotation(f"trace_eval.oracle_summary.{key}",
                                                float(value), source="rules",
                                                identifier=annotation_id("rules")))
    return {
        "key": "__trace__",
        "name": f"trace.eval.{evaluation.get('name') or 'unnamed'}",
        "attributes": {
            "openinference.span.kind": "CHAIN",
            "trace_eval.name": evaluation.get("name") or "unnamed",
            "trace_eval.step_count": len(children),
            "trace_eval.trace_findings": _json(evaluation.get("traceFindings", [])),
            "trace_eval.interpretation": aggregate.get(
                "interpretation", "ordering-only-not-a-probability"),
        },
        "annotations": root_annotations,
        "children": children,
    }


def export_plan(plan: dict, *, collector_endpoint: str = "http://localhost:6006/v1/traces",
                base_url: str = "http://localhost:6006", project_name: str = "trace-eval",
                tracer_provider=None, phoenix_client=None) -> dict:
    """发送 spans 后批量写 annotations；依赖只在真正导出时加载。"""
    if tracer_provider is None:
        try:
            from phoenix.otel import register
        except ImportError as error:
            raise RuntimeError("缺少 Phoenix 导出依赖；执行 pip install -r requirements-phoenix.txt") from error
        tracer_provider = register(endpoint=collector_endpoint, project_name=project_name,
                                   protocol="http/protobuf", batch=False,
                                   set_global_tracer_provider=False, verbose=False)
    tracer = tracer_provider.get_tracer("trace-eval")
    try:
        from opentelemetry.trace import format_span_id
    except ImportError as error:
        raise RuntimeError("缺少 opentelemetry-api") from error

    span_ids = {}
    with tracer.start_as_current_span(plan["name"], attributes=plan["attributes"]) as root:
        span_ids[plan["key"]] = format_span_id(root.get_span_context().span_id)
        for child in plan["children"]:
            with tracer.start_as_current_span(child["name"],
                                              attributes=child["attributes"]) as span:
                span_ids[child["key"]] = format_span_id(span.get_span_context().span_id)
    tracer_provider.force_flush()

    if phoenix_client is None:
        try:
            from phoenix.client import Client
        except ImportError as error:
            raise RuntimeError("缺少 Phoenix client；执行 pip install -r requirements-phoenix.txt") from error
        phoenix_client = Client(base_url=base_url)

    annotations = []
    for item in [plan, *plan["children"]]:
        for annotation in item["annotations"]:
            annotations.append({**annotation, "span_id": span_ids[item["key"]]})
    if annotations:
        phoenix_client.spans.log_span_annotations(span_annotations=annotations, sync=True)
    return {"project": project_name, "spanIds": span_ids,
            "annotationCount": len(annotations), "baseUrl": base_url}


def verify_export(plan: dict, export_result: dict, *, phoenix_client=None) -> dict:
    """从 Phoenix 读回 span/annotations；只有逐项一致才产生 passed proof。"""
    if phoenix_client is None:
        try:
            from phoenix.client import Client
        except ImportError as error:
            raise RuntimeError("缺少 Phoenix client；执行 pip install -r requirements-phoenix.txt") from error
        phoenix_client = Client(base_url=export_result["baseUrl"])
    project = export_result["project"]
    span_ids = export_result["spanIds"]
    expected_spans = set(span_ids.values())
    spans = phoenix_client.spans.get_spans(project_identifier=project,
                                           span_ids=sorted(expected_spans),
                                           limit=max(100, len(expected_spans)))
    observed_spans = set()
    for item in spans:
        value = dict(item) if isinstance(item, dict) else item
        if isinstance(value, dict):
            observed_spans.add(value.get("span_id") or (value.get("context") or {}).get("span_id"))
        else:
            context = getattr(value, "context", None)
            context_span_id = ((context or {}).get("span_id") if isinstance(context, dict)
                               else getattr(context, "span_id", None))
            observed_spans.add(getattr(value, "span_id", None) or context_span_id)
    observed_spans.discard(None)
    annotations = phoenix_client.spans.get_span_annotations(
        project_identifier=project, span_ids=sorted(expected_spans),
        limit=max(1000, export_result["annotationCount"] * 2))

    def normalized(item: dict) -> tuple:
        result = item.get("result") or {}
        return (item.get("span_id"), item.get("name"), item.get("annotator_kind"),
                item.get("identifier"), result.get("label"), result.get("score"),
                result.get("explanation"))

    expected = []
    for item in [plan, *plan["children"]]:
        for annotation in item["annotations"]:
            expected.append(normalized({**annotation, "span_id": span_ids[item["key"]]}))
    expected_identifiers = {row[3] for row in expected}
    observed = [normalized(dict(item)) for item in annotations
                if dict(item).get("identifier") in expected_identifiers]
    missing = sorted(set(expected) - set(observed), key=repr)
    unexpected = sorted(set(observed) - set(expected), key=repr)
    proof_material = json.dumps({"project": project, "spanIds": sorted(expected_spans),
                                 "annotations": sorted(expected, key=repr)},
                                ensure_ascii=False, sort_keys=True, default=str)
    passed = observed_spans >= expected_spans and not missing and not unexpected
    return {"passed": passed, "project": project,
            "expectedSpans": len(expected_spans),
            "observedSpans": len(observed_spans & expected_spans),
            "expectedAnnotations": len(expected), "observedAnnotations": len(observed),
            "missingAnnotations": [list(row) for row in missing],
            "unexpectedAnnotations": [list(row) for row in unexpected],
            "proofHash": hashlib.sha256(proof_material.encode()).hexdigest(),
            "interpretation": "Phoenix write/read verification; not evaluation accuracy"}


def _load(path: str) -> dict:
    value = Path(path)
    value = value / "trace.json" if value.is_dir() else value
    return json.loads(value.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace")
    parser.add_argument("--evaluation", help="trust.evaluate/step_score 产出的 JSON")
    parser.add_argument("--project", default="trace-eval")
    parser.add_argument("--base-url", default="http://localhost:6006")
    parser.add_argument("--collector", default="http://localhost:6006/v1/traces")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true",
                        help="导出后从 Phoenix 读回 spans/annotations 并逐项验证")
    args = parser.parse_args(argv)
    plan = build_phoenix_plan(_load(args.trace), _load(args.evaluation) if args.evaluation else None)
    if args.dry_run:
        if args.verify:
            parser.error("--verify 不能与 --dry-run 同时使用")
        result = plan
    else:
        exported = export_plan(plan, collector_endpoint=args.collector, base_url=args.base_url,
                               project_name=args.project)
        result = {"export": exported,
                  "verification": verify_export(plan, exported)} if args.verify else exported
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.verify and not result["verification"]["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
