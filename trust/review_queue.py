#!/usr/bin/env python3
"""从 challenge/trial 结果生成分层、无真值泄漏的人工盲评队列。"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

QUEUE_SCHEMA = "trace-eval.review-queue/v1"
ANSWER_SCHEMA = "trace-eval.review-answer-key/v1"


def _signals(challenge: dict, trial_rows: list[dict]) -> tuple[int, list[str]]:
    signals, priority = [], 0
    errors = sum(row.get("status") != "success" for row in trial_rows)
    if errors:
        signals.append(f"provider-error:{errors}"); priority += 100 + errors
    exact = [row.get("claimRecordExact") is True for row in trial_rows]
    if len(set(exact)) > 1:
        signals.append("claim-flip"); priority += 70
    if any(row.get("claimDifferences") for row in trial_rows):
        signals.append("claim-mismatch"); priority += 90
    ordering = [row.get("orderingDecreased") for row in trial_rows
                if row.get("orderingDecreased") is not None]
    if ordering and len(set(ordering)) > 1:
        signals.append("ordering-flip"); priority += 60
    if ordering and not any(ordering):
        signals.append("missed-counterfactual"); priority += 80
    unknowns = sum(value == "unknown" for step in challenge["expected"]["stepClaims"]
                   for key, value in step.items() if key != "nodeId")
    if unknowns:
        signals.append(f"evidence-gap:{unknowns}"); priority += min(30, unknowns * 5)
    if challenge.get("baselineId") is not None:
        priority += 10
    return priority, signals or ["coverage-sample"]


def build_review_queue(challenges: list[dict], reports: list[dict], *,
                       limit: int = 20, minimum_per_family: int = 1,
                       include_model_outputs: bool = False) -> dict:
    if limit < 1 or minimum_per_family < 0:
        raise ValueError("limit 必须为正且 minimum_per_family 不能为负")
    challenge_by_id = {row["id"]: row for row in challenges}
    if len(challenge_by_id) != len(challenges):
        raise ValueError("challenge id 不唯一")
    report_indexes = [{row["id"]: row for row in report.get("runs", [])} for report in reports]
    for index in report_indexes:
        if set(index) != set(challenge_by_id):
            raise ValueError("report 与 challenge IDs 不一致")
    candidates = []
    for challenge_id, challenge in challenge_by_id.items():
        trial_rows = [index[challenge_id] for index in report_indexes]
        priority, signals = _signals(challenge, trial_rows)
        candidates.append({"id": challenge_id, "family": challenge["failureFamily"],
                           "priority": priority, "signals": signals,
                           "challenge": challenge, "trialRows": trial_rows})
    candidates.sort(key=lambda row: (-row["priority"], row["id"]))

    selected, selected_ids = [], set()
    by_family = defaultdict(list)
    for row in candidates:
        by_family[row["family"]].append(row)
    # 先保证家族覆盖，再按优先级填满；limit 小于家族数时只能覆盖优先级最高的家族。
    family_heads = sorted((row for values in by_family.values()
                           for row in values[:minimum_per_family]),
                          key=lambda row: (-row["priority"], row["id"]))
    for row in [*family_heads, *candidates]:
        if len(selected) >= limit:
            break
        if row["id"] not in selected_ids:
            selected.append(row); selected_ids.add(row["id"])

    tasks, answers = [], []
    for rank, row in enumerate(selected, 1):
        challenge = row["challenge"]
        # blindPayload 明确不含 challenge.expected。
        blind_payload = {"case": challenge["case"],
                         "description": challenge["description"], **challenge["input"]}
        if include_model_outputs:
            # 仅用于二阶段错误分析；构建人工 gold 的首轮不得开启。
            blind_payload["modelTrials"] = [
                {"status": trial.get("status"), "error": trial.get("error"),
                 "modelOutputs": trial.get("modelOutputs", [])}
                for trial in row["trialRows"]]
        tasks.append({"schema": QUEUE_SCHEMA, "taskId": f"review:{row['id']}",
                      "rank": rank, "priority": row["priority"],
                      "failureFamily": row["family"], "selectionSignals": row["signals"],
                      "instructions": ("独立判断三个 claim；每个 verdict 必须引用截图、DOM、"
                                       "网络、状态查询或 nodeId 证据，不查看 answer key。"),
                      "blindPayload": blind_payload})
        answers.append({"schema": ANSWER_SCHEMA, "taskId": f"review:{row['id']}",
                        "sourceConfidence": challenge["confidence"],
                        "expected": challenge["expected"]})
    family_counts = defaultdict(int)
    for row in selected:
        family_counts[row["family"]] += 1
    return {"queue": tasks, "answerKey": answers,
            "summary": {"candidates": len(candidates), "selected": len(tasks),
                        "familyCounts": dict(sorted(family_counts.items())),
                        "truthExcludedFromBlindPayload": True,
                        "modelOutputsIncluded": include_model_outputs}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("challenges")
    parser.add_argument("reports", nargs="+")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--minimum-per-family", type=int, default=1)
    parser.add_argument("--out", required=True)
    parser.add_argument("--answer-key", required=True)
    parser.add_argument("--include-model-outputs", action="store_true",
                        help="仅用于二阶段错误分析；首轮 gold 盲标不要开启")
    args = parser.parse_args(argv)
    read_jsonl = lambda path: [json.loads(line) for line in Path(path).read_text(
        encoding="utf-8").splitlines() if line.strip()]
    reports = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.reports]
    result = build_review_queue(read_jsonl(args.challenges), reports, limit=args.limit,
                                minimum_per_family=args.minimum_per_family,
                                include_model_outputs=args.include_model_outputs)
    Path(args.out).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                      for row in result["queue"]), encoding="utf-8")
    Path(args.answer_key).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                             for row in result["answerKey"]), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
