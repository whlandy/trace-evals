#!/usr/bin/env python3
"""人工盲标共识与 claim 级 Judge 准确性审计。

分数默认不是概率；只有输入显式 probability 且样本门槛满足时才计算 Brier/ECE。
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from trust.judge import CLAIM_NAMES

VERDICTS = {"pass", "fail", "unknown", "not_applicable"}
BINARY = {"pass", "fail"}
REQUIRED = {"case", "nodeId", "claim", "verdict", "annotator", "evidence"}


def validate_label(row: dict) -> None:
    missing = REQUIRED - set(row)
    if missing:
        raise ValueError(f"人工标签缺字段：{sorted(missing)}")
    if row["claim"] not in CLAIM_NAMES:
        raise ValueError(f"未知 claim：{row['claim']!r}")
    if row["verdict"] not in VERDICTS:
        raise ValueError(f"未知 verdict：{row['verdict']!r}")
    if not isinstance(row["evidence"], list) or not row["evidence"]:
        raise ValueError("人工标签必须提供非空 evidence，不能只给结论")
    if not all(isinstance(item, str) and item.strip() for item in row["evidence"]):
        raise ValueError("evidence 必须是非空字符串数组")
    for key in ("case", "nodeId", "annotator"):
        if not isinstance(row[key], str) or not row[key].strip():
            raise ValueError(f"{key} 必须是非空字符串")


def read_jsonl(path: str | Path) -> list[dict]:
    rows = []
    for line_no, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            validate_label(row)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"{path}:{line_no}: {exc}") from exc
        rows.append(row)
    return rows


def label_consensus(rows: Iterable[dict], *, minimum_annotators: int = 2) -> dict:
    """多数票只在严格多数时形成 gold；平票、人数不足都进入仲裁队列。"""
    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        validate_label(row)
        grouped[(row["case"], row["nodeId"], row["claim"])].append(row)
    gold, adjudication = [], []
    for key, items in sorted(grouped.items()):
        # 同一标注者重复提交不能增加票数，以最后一条为准。
        by_annotator = {item["annotator"]: item for item in items}
        votes = Counter(item["verdict"] for item in by_annotator.values())
        winner, count = votes.most_common(1)[0]
        tied = sum(value == count for value in votes.values()) > 1
        base = {"case": key[0], "nodeId": key[1], "claim": key[2],
                "annotators": sorted(by_annotator), "votes": dict(sorted(votes.items()))}
        if len(by_annotator) < minimum_annotators or tied or count <= len(by_annotator) / 2:
            adjudication.append({**base, "reason": "insufficient-or-no-strict-majority"})
        else:
            evidence = sorted({e for item in by_annotator.values() for e in item["evidence"]})
            gold.append({**base, "verdict": winner, "evidence": evidence})
    return {"gold": gold, "needsAdjudication": adjudication,
            "summary": {"gold": len(gold), "needsAdjudication": len(adjudication)}}


def _cohen_kappa(left: list[str], right: list[str]) -> float | None:
    if not left:
        return None
    observed = sum(a == b for a, b in zip(left, right)) / len(left)
    lc, rc = Counter(left), Counter(right)
    expected = sum((lc[v] / len(left)) * (rc[v] / len(right)) for v in VERDICTS)
    if math.isclose(expected, 1.0):
        return 1.0 if math.isclose(observed, 1.0) else None
    return round((observed - expected) / (1 - expected), 4)


def inter_annotator_agreement(rows: Iterable[dict]) -> dict:
    indexed: dict[str, dict[tuple[str, str, str], str]] = defaultdict(dict)
    for row in rows:
        validate_label(row)
        indexed[row["annotator"]][(row["case"], row["nodeId"], row["claim"])] = row["verdict"]
    names = sorted(indexed)
    def summarize(claim: str | None) -> dict:
        pairs = []
        for i, left_name in enumerate(names):
            for right_name in names[i + 1:]:
                common = sorted(key for key in set(indexed[left_name]) & set(indexed[right_name])
                                if claim is None or key[2] == claim)
                left = [indexed[left_name][key] for key in common]
                right = [indexed[right_name][key] for key in common]
                pairs.append({"annotators": [left_name, right_name], "overlap": len(common),
                              "rawAgreement": (round(sum(a == b for a, b in zip(left, right)) /
                                                     len(common), 4) if common else None),
                              "cohenKappa": _cohen_kappa(left, right)})
        kappas = [item["cohenKappa"] for item in pairs if item["cohenKappa"] is not None]
        return {"pairs": pairs,
                "macroCohenKappa": (round(sum(kappas) / len(kappas), 4)
                                    if kappas else None)}
    overall = summarize(None)
    return {**overall, "byClaim": {claim: summarize(claim) for claim in CLAIM_NAMES},
            "interpretation": "agreement diagnostic; adjudicate disagreements before gold"}


def _wilson_interval(successes: int, total: int, *, z: float = 1.96) -> list[float] | None:
    if total <= 0:
        return None
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(proportion * (1 - proportion) / total
                           + z * z / (4 * total * total)) / denominator
    return [round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4)]


def _binary_metrics(rows: list[tuple[str, str]]) -> dict:
    tp = sum(pred == gold == "pass" for pred, gold in rows)
    fp = sum(pred == "pass" and gold == "fail" for pred, gold in rows)
    fn = sum(pred == "fail" and gold == "pass" for pred, gold in rows)
    tn = sum(pred == gold == "fail" for pred, gold in rows)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision is not None and recall is not None and precision + recall else None)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "accuracy": round((tp + tn) / len(rows), 4) if rows else None,
            "precision": round(precision, 4) if precision is not None else None,
            "recall": round(recall, 4) if recall is not None else None,
            "f1": round(f1, 4) if f1 is not None else None}


def evaluate_claims(predictions: Iterable[dict], gold: Iterable[dict]) -> dict:
    gold_index = {(r["case"], r["nodeId"], r["claim"]): r for r in gold
                  if r["verdict"] in BINARY}
    prediction_index = {}
    duplicates = []
    for pred in predictions:
        key = (pred.get("case"), pred.get("nodeId"), pred.get("claim"))
        if key in prediction_index:
            duplicates.append(key)
        prediction_index[key] = pred
    # gold 决定分母；漏预测不能让困难样本从准确率审计中消失。
    matched = [(prediction_index.get(key), row) for key, row in gold_index.items()]
    by_claim = {}
    for claim in CLAIM_NAMES:
        items = [(p, g) for p, g in matched if g["claim"] == claim]
        known = [(p["verdict"], g["verdict"]) for p, g in items
                 if p is not None and p.get("verdict") in BINARY]
        metrics = _binary_metrics(known)
        correct = metrics["tp"] + metrics["tn"]
        by_claim[claim] = {**metrics, "goldCount": len(items),
                           "knownPredictions": len(known),
                           "coverage": round(len(known) / len(items), 4) if items else None,
                           "abstentions": len(items) - len(known),
                           "selectiveAccuracy": metrics["accuracy"],
                           "selectiveAccuracyWilson95": _wilson_interval(correct, len(known)),
                           "effectiveAccuracy": (round(correct / len(items), 4)
                                                 if items else None),
                           "effectiveAccuracyWilson95": _wilson_interval(correct, len(items))}
    f1s = [row["f1"] for row in by_claim.values() if row["f1"] is not None]
    total = sum(row["goldCount"] for row in by_claim.values())
    known = sum(row["knownPredictions"] for row in by_claim.values())
    correct = sum(row["tp"] + row["tn"] for row in by_claim.values())
    return {"byClaim": by_claim,
            "overall": {"goldCount": total, "knownPredictions": known,
                        "coverage": round(known / total, 4) if total else None,
                        "selectiveAccuracy": (round(correct / known, 4) if known else None),
                        "selectiveAccuracyWilson95": _wilson_interval(correct, known),
                        "effectiveAccuracy": (round(correct / total, 4) if total else None),
                        "effectiveAccuracyWilson95": _wilson_interval(correct, total),
                        "macroF1": round(sum(f1s) / len(f1s), 4) if f1s else None,
                        "duplicatePredictionKeys": len(duplicates)},
            "interpretation": "unknown/not_applicable are abstentions, not hidden errors or passes"}


def probability_calibration(predictions: Iterable[dict], gold: Iterable[dict], *,
                            minimum_samples: int = 30, bins: int = 10) -> dict:
    """仅接受显式 probability；门槛不足时拒绝产生貌似精确的校准数。"""
    gold_index = {(r["case"], r["nodeId"], r["claim"]): r["verdict"] for r in gold}
    samples = []
    for pred in predictions:
        key = (pred.get("case"), pred.get("nodeId"), pred.get("claim"))
        verdict = gold_index.get(key)
        probability = pred.get("probability")
        if verdict in BINARY and isinstance(probability, (int, float)) and not isinstance(probability, bool):
            if not 0 <= probability <= 1:
                raise ValueError("probability 必须在 [0, 1]")
            samples.append((float(probability), 1 if verdict == "pass" else 0))
    classes = {truth for _, truth in samples}
    if len(samples) < minimum_samples or classes != {0, 1}:
        return {"status": "insufficient-data", "samples": len(samples),
                "minimumSamples": minimum_samples, "classes": sorted(classes),
                "reason": "requires explicit probabilities, enough labels, and both classes"}
    brier = sum((prob - truth) ** 2 for prob, truth in samples) / len(samples)
    buckets = [[] for _ in range(bins)]
    for sample in samples:
        buckets[min(int(sample[0] * bins), bins - 1)].append(sample)
    ece = 0.0
    for bucket in buckets:
        if bucket:
            confidence = sum(p for p, _ in bucket) / len(bucket)
            accuracy = sum(y for _, y in bucket) / len(bucket)
            ece += len(bucket) / len(samples) * abs(confidence - accuracy)
    return {"status": "computed", "samples": len(samples),
            "brier": round(brier, 6), "ece": round(ece, 6), "bins": bins,
            "interpretation": "probability calibration, not ordering-score validation"}


def calibration_readiness(rows: Iterable[dict], *, minimum_annotators: int = 2,
                          minimum_total_gold: int = 50,
                          minimum_gold_per_claim: int = 30,
                          minimum_agreement_overlap: int = 30,
                          minimum_cohen_kappa: float = 0.6) -> dict:
    """人工 gold 是否足以进入 claim 准确性/校准阶段；不生成任何模型分。"""
    if minimum_annotators < 2:
        raise ValueError("minimum_annotators 至少为 2")
    if minimum_total_gold < 1 or minimum_gold_per_claim < 1:
        raise ValueError("gold 样本门槛必须为正整数")
    if minimum_agreement_overlap < 1:
        raise ValueError("minimum_agreement_overlap 必须为正整数")
    if not -1 <= minimum_cohen_kappa <= 1:
        raise ValueError("minimum_cohen_kappa 必须在 [-1, 1]")
    rows = list(rows)
    consensus = label_consensus(rows, minimum_annotators=minimum_annotators)
    agreement = inter_annotator_agreement(rows)
    by_claim, failures = {}, []
    for claim in CLAIM_NAMES:
        items = [row for row in consensus["gold"] if row["claim"] == claim]
        classes = Counter(row["verdict"] for row in items if row["verdict"] in BINARY)
        ready = len(items) >= minimum_gold_per_claim and set(classes) == BINARY
        by_claim[claim] = {"gold": len(items), "classes": dict(sorted(classes.items())),
                           "minimumGold": minimum_gold_per_claim, "ready": ready}
        if len(items) < minimum_gold_per_claim:
            failures.append(f"{claim}:insufficient-gold")
        if set(classes) != BINARY:
            failures.append(f"{claim}:requires-pass-and-fail")
        pairs = agreement["byClaim"][claim]["pairs"]
        eligible = [pair for pair in pairs
                    if pair["overlap"] >= minimum_agreement_overlap
                    and pair["cohenKappa"] is not None]
        agreement_ready = bool(eligible) and min(
            pair["cohenKappa"] for pair in eligible) >= minimum_cohen_kappa
        by_claim[claim]["agreement"] = {
            "eligiblePairs": len(eligible), "minimumOverlap": minimum_agreement_overlap,
            "minimumCohenKappa": minimum_cohen_kappa,
            "lowestEligibleKappa": (min(pair["cohenKappa"] for pair in eligible)
                                    if eligible else None),
            "ready": agreement_ready}
        by_claim[claim]["ready"] = ready and agreement_ready
        if not eligible:
            failures.append(f"{claim}:insufficient-agreement-overlap")
        elif not agreement_ready:
            failures.append(f"{claim}:low-agreement")
    if consensus["needsAdjudication"]:
        failures.append("unresolved-adjudication")
    if len(consensus["gold"]) < minimum_total_gold:
        failures.append("insufficient-total-gold")
    return {"ready": not failures, "failures": failures,
            "gold": len(consensus["gold"]),
            "needsAdjudication": len(consensus["needsAdjudication"]),
            "minimumAnnotators": minimum_annotators,
            "minimumTotalGold": minimum_total_gold,
            "minimumAgreementOverlap": minimum_agreement_overlap,
            "minimumCohenKappa": minimum_cohen_kappa,
            "byClaim": by_claim,
            "interpretation": "human-gold readiness gate; not model accuracy or probability"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("labels", help="人工盲标 JSONL")
    parser.add_argument("--predictions", help="Judge claim JSONL")
    parser.add_argument("--minimum-annotators", type=int, default=2)
    parser.add_argument("--minimum-total-gold", type=int, default=50)
    parser.add_argument("--minimum-gold-per-claim", type=int, default=30)
    parser.add_argument("--minimum-agreement-overlap", type=int, default=30)
    parser.add_argument("--minimum-cohen-kappa", type=float, default=0.6)
    args = parser.parse_args(argv)
    labels = read_jsonl(args.labels)
    consensus = label_consensus(labels, minimum_annotators=args.minimum_annotators)
    readiness = calibration_readiness(
        labels, minimum_annotators=args.minimum_annotators,
        minimum_total_gold=args.minimum_total_gold,
        minimum_gold_per_claim=args.minimum_gold_per_claim,
        minimum_agreement_overlap=args.minimum_agreement_overlap,
        minimum_cohen_kappa=args.minimum_cohen_kappa)
    report = {"agreement": inter_annotator_agreement(labels), "consensus": consensus,
              "readiness": readiness}
    if args.predictions:
        predictions = [json.loads(line) for line in Path(args.predictions).read_text(
            encoding="utf-8").splitlines() if line.strip()]
        report["accuracy"] = evaluate_claims(predictions, consensus["gold"])
        report["calibration"] = probability_calibration(predictions, consensus["gold"])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if readiness["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
