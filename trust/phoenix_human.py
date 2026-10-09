#!/usr/bin/env python3
"""把逐 annotator 盲标作为 Phoenix HUMAN span annotations 写回。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from trust.calibrate import read_jsonl, validate_label
from trust.judge import RUBRIC_VERSION


def _annotator_identifier(annotator: str) -> str:
    # identifier 用于 Phoenix 幂等 upsert；不把原始 annotator 名放进主键。
    digest = hashlib.sha256(annotator.encode()).hexdigest()[:16]
    return f"trace-eval-human:{digest}"


def build_human_annotation_plan(labels: list[dict], span_ids: dict[str, dict[str, str]], *,
                                rubric_version: str = RUBRIC_VERSION) -> list[dict]:
    annotations = []
    seen = set()
    for row in labels:
        validate_label(row)
        case, node_id = row["case"], row["nodeId"]
        span_id = (span_ids.get(case) or {}).get(node_id)
        if not isinstance(span_id, str) or not span_id.strip():
            raise ValueError(f"找不到人工标签对应 span：{case}/{node_id}")
        key = (span_id, row["claim"], row["annotator"])
        if key in seen:
            raise ValueError(f"同一 annotator 重复标签：{case}/{node_id}/{row['claim']}")
        seen.add(key)
        annotations.append({
            "span_id": span_id,
            "name": f"trace_eval.human.{row['claim']}",
            "annotator_kind": "HUMAN",
            "result": {"label": row["verdict"],
                       "explanation": "evidence: " + ", ".join(row["evidence"])},
            "identifier": _annotator_identifier(row["annotator"]),
            "metadata": {"source": "blind-human-label", "case": case,
                         "nodeId": node_id, "claim": row["claim"],
                         "annotator": row["annotator"],
                         "rubricVersion": rubric_version,
                         "evidence": row["evidence"],
                         "interpretation": "human categorical judgment; not probability"},
        })
    return annotations


def log_human_annotations(annotations: list[dict], *, base_url: str = "http://localhost:6006",
                          phoenix_client=None) -> dict:
    if not annotations:
        raise ValueError("拒绝写入空 HUMAN annotation 集合")
    if phoenix_client is None:
        try:
            from phoenix.client import Client
        except ImportError as error:
            raise RuntimeError("缺少 Phoenix client；执行 pip install -r requirements-phoenix.txt") from error
        phoenix_client = Client(base_url=base_url)
    phoenix_client.spans.log_span_annotations(span_annotations=annotations, sync=True)
    return {"annotationCount": len(annotations), "baseUrl": base_url,
            "annotatorKind": "HUMAN"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("labels", help="逐 annotator 双人盲标 JSONL")
    parser.add_argument("span_ids", help="{case: {nodeId: Phoenix spanId}} JSON")
    parser.add_argument("--base-url", default="http://localhost:6006")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    plan = build_human_annotation_plan(
        read_jsonl(args.labels), json.loads(Path(args.span_ids).read_text(encoding="utf-8")))
    result = plan if args.dry_run else log_human_annotations(plan, base_url=args.base_url)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
