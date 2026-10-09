import json

import pytest

from trust.calibrate import (calibration_readiness, evaluate_claims,
                             inter_annotator_agreement, label_consensus,
                             main, probability_calibration, validate_label)


def _label(annotator, verdict, *, claim="target_execution", node="n1"):
    return {"case": "c1", "nodeId": node, "claim": claim, "verdict": verdict,
            "annotator": annotator, "evidence": [f"screenshot:{node}"]}


def test_labels_require_evidence_and_known_claim():
    row = _label("a", "pass")
    row["evidence"] = []
    with pytest.raises(ValueError, match="evidence"):
        validate_label(row)
    row = _label("a", "pass")
    row["claim"] = "vibes"
    with pytest.raises(ValueError, match="claim"):
        validate_label(row)


def test_consensus_requires_two_people_and_strict_majority():
    rows = [_label("a", "pass"), _label("b", "pass"), _label("c", "fail")]
    result = label_consensus(rows)
    assert result["gold"][0]["verdict"] == "pass"
    tied = label_consensus([_label("a", "pass"), _label("b", "fail")])
    assert not tied["gold"]
    assert tied["summary"]["needsAdjudication"] == 1


def test_duplicate_annotator_does_not_manufacture_majority():
    rows = [_label("a", "fail"), _label("a", "pass"), _label("b", "fail")]
    result = label_consensus(rows)
    assert not result["gold"]


def test_agreement_reports_overlap_and_kappa():
    rows = [_label("a", "pass"), _label("b", "pass"),
            _label("a", "fail", node="n2"), _label("b", "fail", node="n2")]
    result = inter_annotator_agreement(rows)
    assert result["pairs"][0]["overlap"] == 2
    assert result["pairs"][0]["cohenKappa"] == 1.0
    assert result["byClaim"]["target_execution"]["macroCohenKappa"] == 1.0
    assert result["byClaim"]["post_action_reaction"]["pairs"][0]["overlap"] == 0


def test_claim_metrics_keep_abstention_visible():
    gold = [
        {**_label("gold", "pass"), "verdict": "pass"},
        {**_label("gold", "fail", node="n2"), "verdict": "fail"},
    ]
    predictions = [
        {"case": "c1", "nodeId": "n1", "claim": "target_execution", "verdict": "pass"},
        {"case": "c1", "nodeId": "n2", "claim": "target_execution", "verdict": "unknown"},
    ]
    result = evaluate_claims(predictions, gold)
    metric = result["byClaim"]["target_execution"]
    assert metric["accuracy"] == 1.0
    assert metric["coverage"] == 0.5
    assert metric["abstentions"] == 1
    assert metric["selectiveAccuracy"] == 1.0
    assert metric["effectiveAccuracy"] == 0.5
    assert metric["selectiveAccuracyWilson95"] == [0.2065, 1.0]
    assert metric["effectiveAccuracyWilson95"] == [0.0945, 0.9055]
    assert result["overall"]["selectiveAccuracy"] == 1.0
    assert result["overall"]["effectiveAccuracy"] == 0.5


def test_missing_prediction_stays_in_gold_denominator():
    gold = [{**_label("gold", "pass"), "verdict": "pass"}]
    result = evaluate_claims([], gold)
    metric = result["byClaim"]["target_execution"]
    assert metric["goldCount"] == 1
    assert metric["coverage"] == 0.0
    assert metric["abstentions"] == 1


def test_probability_metrics_are_gated_and_never_use_ordering_scores():
    gold = [{**_label("gold", "pass"), "verdict": "pass"},
            {**_label("gold", "fail", node="n2"), "verdict": "fail"}]
    ordering_only = [{"case": "c1", "nodeId": "n1", "claim": "target_execution",
                      "overall": 0.9}]
    assert probability_calibration(ordering_only, gold, minimum_samples=1)["status"] == "insufficient-data"
    predictions = [
        {"case": "c1", "nodeId": "n1", "claim": "target_execution", "probability": 0.8},
        {"case": "c1", "nodeId": "n2", "claim": "target_execution", "probability": 0.2},
    ]
    result = probability_calibration(predictions, gold, minimum_samples=2, bins=2)
    assert result["status"] == "computed"
    assert result["brier"] == 0.04
    assert result["ece"] == 0.2


def test_calibration_readiness_requires_consensus_each_claim_and_both_classes():
    rows = []
    for claim in ("target_execution", "post_action_reaction", "testcase_conformance"):
        for node, verdict in (("pass-node", "pass"), ("fail-node", "fail")):
            rows.extend([_label("a", verdict, claim=claim, node=node),
                         _label("b", verdict, claim=claim, node=node)])
    report = calibration_readiness(rows, minimum_total_gold=6,
                                   minimum_gold_per_claim=2,
                                   minimum_agreement_overlap=2)
    assert report["ready"] is True
    assert report["gold"] == 6
    assert all(set(item["classes"]) == {"pass", "fail"}
               for item in report["byClaim"].values())
    assert all(item["agreement"]["ready"] for item in report["byClaim"].values())


def test_calibration_readiness_fails_on_sparse_or_unresolved_labels():
    rows = [_label("a", "pass"), _label("b", "fail")]
    report = calibration_readiness(rows, minimum_total_gold=1,
                                   minimum_gold_per_claim=1,
                                   minimum_agreement_overlap=1)
    assert report["ready"] is False
    assert "unresolved-adjudication" in report["failures"]
    assert any(value.endswith("requires-pass-and-fail") for value in report["failures"])


def test_calibration_cli_exit_code_is_a_readiness_gate(tmp_path, capsys):
    path = tmp_path / "labels.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in
                              [_label("a", "pass"), _label("b", "fail")]) + "\n")
    code = main([str(path), "--minimum-total-gold", "1",
                 "--minimum-gold-per-claim", "1",
                 "--minimum-agreement-overlap", "1"])
    assert code == 1
    assert '"ready": false' in capsys.readouterr().out


def test_readiness_rejects_claim_specific_low_agreement():
    rows = []
    for claim in ("target_execution", "post_action_reaction", "testcase_conformance"):
        for node, verdict in (("n1", "pass"), ("n2", "fail"),
                              ("n3", "pass"), ("n4", "fail")):
            rows.append(_label("a", verdict, claim=claim, node=node))
            other = (("fail" if verdict == "pass" else "pass")
                     if claim == "post_action_reaction" else verdict)
            rows.append(_label("b", other, claim=claim, node=node))
    report = calibration_readiness(rows, minimum_total_gold=1,
                                   minimum_gold_per_claim=1,
                                   minimum_agreement_overlap=4,
                                   minimum_cohen_kappa=0.6)
    assert report["ready"] is False
    assert "post_action_reaction:low-agreement" in report["failures"]
    assert report["byClaim"]["post_action_reaction"]["agreement"]["ready"] is False
