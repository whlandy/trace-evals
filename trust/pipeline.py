#!/usr/bin/env python3
"""面向生产批处理的最小入口：加载 case、隔离错误、输出评估与复核信号。"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from trust.evaluate import evaluate_trace
from trust.providers import OpenAIProvider

CASE_SCHEMA = "trace-eval.case/v1"
REQUIRED = {"schema", "case", "goal", "trace"}
OPTIONAL = {"reference", "observations", "oracleSpec"}


@dataclass(frozen=True)
class ManifestError:
    source: str
    case: str | None
    error: Exception


def validate_case(value: dict) -> None:
    if not isinstance(value, dict) or set(value) - (REQUIRED | OPTIONAL) or not REQUIRED <= set(value):
        raise ValueError("case 字段必须为 schema/case/goal/trace 及可选 reference/observations/oracleSpec")
    if value["schema"] != CASE_SCHEMA:
        raise ValueError(f"不支持的 case schema：{value['schema']!r}")
    for key in ("case", "goal"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError(f"case.{key} 必须为非空字符串")
    for key in ("trace", "reference", "observations", "oracleSpec"):
        if key in value and (key == "trace" or value[key] is not None) and not isinstance(value[key], dict):
            raise ValueError(f"case.{key} 必须为对象")


def review_reasons(evaluation: dict) -> list[str]:
    reasons = []
    verdicts = [claim["verdict"] for step in evaluation["steps"]
                for claim in step.get("claims", {}).values()]
    if "fail" in verdicts:
        reasons.append("claim-fail")
    if "unknown" in verdicts:
        reasons.append("claim-unknown")
    flow = [check["verdict"] for check in evaluation["oracle"]["flowChecks"]]
    if "fail" in flow:
        reasons.append("flow-fail")
    if "unknown" in flow:
        reasons.append("flow-unknown")
    if evaluation["aggregate"].get("confidence") != "calibrated":
        reasons.append("uncalibrated-score")
    return reasons


def evaluate_cases(cases: list, *, provider, cache_dir: str | Path | None = None) -> dict:
    ids = []
    for case in cases:
        try:
            validate_case(case)
        except ValueError:
            continue
        ids.append(case["case"])
    if len(ids) != len(set(ids)):
        raise ValueError("case id 必须唯一")
    records = []
    for index, value in enumerate(cases):
        try:
            if isinstance(value, ManifestError):
                raise value.error
            validate_case(value)
            evaluation = evaluate_trace(
                value["trace"], goal=value["goal"], provider=provider,
                reference=value.get("reference"), observations=value.get("observations"),
                oracle_spec=value.get("oracleSpec"), cache_dir=cache_dir)
            reasons = review_reasons(evaluation)
            records.append({"case": value["case"], "status": "success",
                            "needsReview": bool(reasons), "reviewReasons": reasons,
                            "evaluation": evaluation})
        except Exception as error:
            case_id = (value.case if isinstance(value, ManifestError) else
                       value.get("case") if isinstance(value, dict) else None)
            records.append({"case": case_id if isinstance(case_id, str) else None,
                            "inputIndex": index,
                            **({"source": value.source} if isinstance(value, ManifestError) else {}),
                            "status": "evaluation-error",
                            "needsReview": True, "reviewReasons": ["evaluation-error"],
                            "errorType": type(error).__name__, "error": str(error)})
    statuses = Counter(row["status"] for row in records)
    reason_counts = Counter(reason for row in records for reason in row["reviewReasons"])
    return {"schema": "trace-eval.pipeline-report/v1", "provider": provider.identity,
            "records": records,
            "summary": {"cases": len(records), "successful": statuses["success"],
                        "errors": statuses["evaluation-error"],
                        "needsReview": sum(row["needsReview"] for row in records),
                        "reviewReasons": dict(sorted(reason_counts.items())),
                        "interpretation": "evaluation routing; not model accuracy or probability"}}


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_case_manifest(path: str | Path) -> list[dict | ManifestError]:
    manifest = Path(path).resolve()
    rows = []
    for line_no, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = None
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("case 必须为对象")
            for key in ("trace", "reference", "observations", "oracleSpec"):
                if isinstance(row.get(key), str):
                    source = (manifest.parent / row[key]).resolve()
                    row[key] = _load_json(source)
            validate_case(row)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            case_id = row.get("case") if isinstance(row, dict) else None
            rows.append(ManifestError(f"{manifest}:{line_no}",
                                      case_id if isinstance(case_id, str) else None, error))
            continue
        rows.append(row)
    if not rows:
        raise ValueError("case manifest 不能为空")
    return rows


def _write_report(path: str | Path, report: dict) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", help="trace-eval.case/v1 JSONL")
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning-effort", default="low")
    parser.add_argument("--cache-dir", default=".trust-judge-cache")
    parser.add_argument("--out", required=True)
    parser.add_argument("--fail-on-review", action="store_true")
    args = parser.parse_args(argv)
    report = evaluate_cases(
        read_case_manifest(args.manifest),
        provider=OpenAIProvider(args.model, reasoning_effort=args.reasoning_effort),
        cache_dir=args.cache_dir)
    _write_report(args.out, report)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    if report["summary"]["errors"]:
        return 1
    return 1 if args.fail_on_review and report["summary"]["needsReview"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
