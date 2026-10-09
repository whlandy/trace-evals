#!/usr/bin/env python3
"""把 trace + 人工 gold 变成 Phoenix golden dataset，并比较 Judge 实验。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Iterable

from trust.calibrate import evaluate_claims, label_consensus, read_jsonl
from trust.judge import RUBRIC_VERSION, build_payload


def _stable_id(case: str, node_id: str, claim: str) -> str:
    material = f"{case}\0{node_id}\0{claim}".encode()
    return hashlib.sha256(material).hexdigest()[:24]


def _content_hash(value: dict | None) -> str | None:
    if value is None:
        return None
    material = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode()
    return hashlib.sha256(material).hexdigest()


def build_dataset_examples(cases: dict[str, dict], gold: Iterable[dict], *,
                           goal: str,
                           oracle_specs: dict[str, dict] | None = None) -> list[dict]:
    """每个 claim 是一个 example，避免把三个不同问题压成一个总分。"""
    oracle_specs = oracle_specs or {}
    unknown_specs = set(oracle_specs) - set(cases)
    if unknown_specs:
        raise ValueError(f"oracle spec 引用了不存在的 case：{sorted(unknown_specs)}")
    payloads = {case: build_payload(trace, goal=goal,
                                    oracle_spec=oracle_specs.get(case))
                for case, trace in cases.items()}
    examples = []
    for row in sorted(gold, key=lambda x: (x["case"], x["nodeId"], x["claim"])):
        case, node_id, claim = row["case"], row["nodeId"], row["claim"]
        if case not in payloads:
            raise ValueError(f"gold 引用了不存在的 case：{case}")
        context = next((item for item in payloads[case]["steps"]
                        if item["current"]["nodeId"] == node_id), None)
        if context is None:
            raise ValueError(f"gold 引用了不存在的 nodeId：{case}/{node_id}")
        examples.append({
            "example_id": _stable_id(case, node_id, claim),
            "input": {"goal": goal, "claim": claim, "stepContext": context,
                      "flowOracle": payloads[case]["flowOracle"],
                      "nodeOracleSpec": ((oracle_specs.get(case, {}).get("nodes") or {})
                                         .get(node_id))},
            "output": {"verdict": row["verdict"], "evidence": row["evidence"]},
            "metadata": {"case": case, "nodeId": node_id, "claim": claim,
                         "rubricVersion": RUBRIC_VERSION,
                         "oracleSpecHash": _content_hash(oracle_specs.get(case)),
                         "annotators": row.get("annotators", []),
                         "votes": row.get("votes", {})},
        })
    return examples


def upload_dataset(examples: list[dict], *, name: str, description: str = "",
                   phoenix_client=None, dataframe_factory=None):
    if not examples:
        raise ValueError("不能上传空 golden dataset")
    if phoenix_client is None:
        try:
            from phoenix.client import Client
        except ImportError as error:
            raise RuntimeError("缺少 Phoenix client；执行 pip install -r requirements-phoenix.txt") from error
        phoenix_client = Client()
    if dataframe_factory is None:
        try:
            from pandas import DataFrame
        except ImportError as error:
            raise RuntimeError("稳定更新 Phoenix dataset 需要 pandas；执行 pip install -r requirements-phoenix.txt") from error
        dataframe_factory = DataFrame
    rows = [{"example_id": item["example_id"],
             **item["input"], **item["output"], **item["metadata"]}
            for item in examples]
    dataframe = dataframe_factory(rows)
    dataset = phoenix_client.datasets.create_dataset(
        name=name,
        dataset_description=description or "Trace action/reaction/flow human-gold claims",
        dataframe=dataframe,
        input_keys=["goal", "claim", "stepContext", "flowOracle", "nodeOracleSpec"],
        output_keys=["verdict", "evidence"],
        metadata_keys=["case", "nodeId", "rubricVersion", "oracleSpecHash",
                       "annotators", "votes"],
        example_id_key="example_id",
    )
    return {"name": getattr(dataset, "name", name),
            "versionId": getattr(dataset, "version_id", None),
            "exampleCount": getattr(dataset, "example_count", len(examples))}


def _metric_deltas(candidate: dict, baseline: dict) -> dict:
    deltas = {}
    for claim, candidate_row in candidate["byClaim"].items():
        base_row = baseline["byClaim"][claim]
        deltas[claim] = {
            key: (round(candidate_row[key] - base_row[key], 4)
                  if candidate_row[key] is not None and base_row[key] is not None else None)
            for key in ("f1", "precision", "recall", "coverage")
        }
    return deltas


def compare_judges(baseline_predictions: Iterable[dict], candidate_predictions: Iterable[dict],
                   gold: Iterable[dict], *, max_f1_regression: float = 0.0,
                   max_coverage_regression: float = 0.0) -> dict:
    """逐 claim 回归门；缺指标不会被当成通过。"""
    gold = list(gold)
    baseline = evaluate_claims(baseline_predictions, gold)
    candidate = evaluate_claims(candidate_predictions, gold)
    deltas = _metric_deltas(candidate, baseline)
    checks = []
    for claim, row in deltas.items():
        # 数据集可以分批建设；只 gate 当前 gold 中实际存在的 claim。
        if baseline["byClaim"][claim]["goldCount"] == 0:
            continue
        for metric, allowed in (("f1", max_f1_regression),
                                ("coverage", max_coverage_regression)):
            delta = row[metric]
            checks.append({"claim": claim, "metric": metric, "delta": delta,
                           "allowedRegression": allowed,
                           "passed": delta is not None and delta >= -allowed})
    return {"passed": all(item["passed"] for item in checks), "checks": checks,
            "deltas": deltas, "baseline": baseline, "candidate": candidate,
            "interpretation": "paired claim-level regression gate; no probability semantics"}


def _load_trace(path: str) -> dict:
    value = Path(path)
    value = value / "trace.json" if value.is_dir() else value
    return json.loads(value.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("labels", help="双人盲标 JSONL")
    parser.add_argument("--case", action="append", nargs=2, metavar=("NAME", "TRACE"), required=True)
    parser.add_argument("--oracle-spec", action="append", nargs=2,
                        metavar=("CASE", "SPEC"), help="某 case 的扩展 oracle spec")
    parser.add_argument("--goal", required=True)
    parser.add_argument("--name", default="trace-eval-human-gold")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    consensus = label_consensus(read_jsonl(args.labels))
    if consensus["needsAdjudication"]:
        raise SystemExit("存在未仲裁标签；拒绝上传不确定 gold")
    examples = build_dataset_examples(
        {name: _load_trace(path) for name, path in args.case}, consensus["gold"], goal=args.goal,
        oracle_specs={name: _load_trace(path) for name, path in (args.oracle_spec or [])})
    result = examples if args.dry_run else upload_dataset(examples, name=args.name)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
