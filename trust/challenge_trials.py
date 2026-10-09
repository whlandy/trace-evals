#!/usr/bin/env python3
"""汇总多次独立 challenge run 的 pass@k、pass^k 与翻转率。"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _summarize(rows: list[dict]) -> dict:
    claim_trials = [row["claimOutcomes"] for row in rows]
    ordering_trials = [row["orderingOutcomes"] for row in rows if row["orderingOutcomes"]]
    return {
        "records": len(rows),
        "claimPassAtK": _rate([any(values) for values in claim_trials]),
        "claimPassPowerK": _rate([all(values) for values in claim_trials]),
        "claimFlipRate": _rate([len(set(values)) > 1 for values in claim_trials]),
        "orderingPassAtK": _rate([any(values) for values in ordering_trials]),
        "orderingPassPowerK": _rate([all(values) for values in ordering_trials]),
        "orderingFlipRate": _rate([len(set(values)) > 1 for values in ordering_trials]),
        "providerErrorTrialRate": (
            round(sum(row["providerErrors"] for row in rows)
                  / sum(row["trials"] for row in rows), 4) if rows else None),
    }


def _stability_gate(aggregate: dict) -> dict:
    failures = []
    for key in ("claimPassPowerK", "orderingPassPowerK"):
        if aggregate.get(key) != 1.0:
            failures.append(f"{key}-not-one")
    for key in ("claimFlipRate", "orderingFlipRate", "providerErrorTrialRate"):
        if aggregate.get(key) != 0.0:
            failures.append(f"{key}-not-zero")
    return {"passed": not failures, "failures": failures,
            "policy": "all synthetic contracts pass in every independent trial with zero flips",
            "interpretation": "stability contract; not deployment success probability"}


def analyze_trials(reports: list[dict]) -> dict:
    if len(reports) < 2:
        raise ValueError("稳定性分析至少需要 2 份独立 challenge report")
    providers = {report.get("provider") for report in reports}
    if len(providers) != 1:
        raise ValueError(f"trial provider 不一致：{sorted(str(x) for x in providers)}")
    prompt_ids = {report.get("promptId") for report in reports}
    prompt_hashes = {report.get("promptHash") for report in reports}
    if None in prompt_ids or None in prompt_hashes or len(prompt_ids) != 1 or len(prompt_hashes) != 1:
        raise ValueError("随机 trial 必须使用相同且明确的 promptId/promptHash")
    indexes = [{row["id"]: row for row in report.get("runs", [])} for report in reports]
    id_sets = [set(index) for index in indexes]
    if any(ids != id_sets[0] for ids in id_sets[1:]):
        raise ValueError("trial challenge IDs 不一致")
    rows = []
    rubric_versions = set()
    for challenge_id in sorted(id_sets[0]):
        trials = [index[challenge_id] for index in indexes]
        families = {row["family"] for row in trials}
        mutations = {row["mutation"] for row in trials}
        if len(families) != 1 or len(mutations) != 1:
            raise ValueError(f"trial 元数据不一致：{challenge_id}")
        versions = {row.get("rubricVersion") for row in trials if row.get("rubricVersion")}
        rubric_versions |= versions
        claim = [row.get("status") == "success" and row.get("claimRecordExact") is True
                 for row in trials]
        ordering = ([row.get("orderingDecreased") is True for row in trials]
                    if trials[0].get("baselineId") is not None else [])
        rows.append({"id": challenge_id, "family": next(iter(families)),
                     "mutation": next(iter(mutations)), "trials": len(trials),
                     "claimOutcomes": claim, "orderingOutcomes": ordering,
                     "providerErrors": sum(row.get("status") != "success" for row in trials)})
    if len(rubric_versions) > 1:
        raise ValueError(f"trial rubricVersion 不一致：{sorted(rubric_versions)}")
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["family"]].append(row)
    aggregate = _summarize(rows)
    return {"provider": next(iter(providers)),
            "promptId": next(iter(prompt_ids)), "promptHash": next(iter(prompt_hashes)),
            "rubricVersion": next(iter(rubric_versions), None),
            "k": len(reports), "records": rows, "aggregate": aggregate,
            "stabilityGate": _stability_gate(aggregate),
            "byFailureFamily": {family: _summarize(values)
                                for family, values in sorted(grouped.items())},
            "interpretation": (
                "empirical independent trials: pass@k rewards any success; pass^k requires all "
                "successes; these metrics are not a calibrated deployment probability")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", nargs="+")
    args = parser.parse_args(argv)
    reports = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.reports]
    report = analyze_trials(reports)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["stabilityGate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
