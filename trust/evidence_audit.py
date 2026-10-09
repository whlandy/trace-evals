#!/usr/bin/env python3
"""审计 Judge 证据的回链精度、具体性和反事实归因敏感性。

这些指标描述 evidence 行为，不是任务成功率，也不是概率校准。
"""

from __future__ import annotations

from typing import Iterable

from trust.judge import SCORE_DIMENSIONS, build_payload

TRAJECTORY_DIMENSIONS = ("completeness", "necessity", "ordering", "evidence_closure")
STEP_FIELDS = {
    "action_correctness": {"current.action", "current.arguments"},
    "argument_quality": {"current.selector", "current.arguments", "current.responses"},
    "context_fit": {"before", "after", "flowOracle"},
    "evidence_quality": {"current.assertion", "current.responses", "oracleChecks"},
    "replay_safety": {"current.selector", "current.arguments", "ruleFindings"},
}
TRAJECTORY_FIELDS = {
    "completeness": {"referenceSteps", "flowOracle", "traceFindings"},
    "necessity": {"steps", "referenceSteps", "traceFindings"},
    "ordering": {"referenceSteps", "flowOracle"},
    "evidence_closure": {"flowOracle", "traceFindings", "oracleChecks"},
}
GENERIC_TOKENS = set().union(*STEP_FIELDS.values(), *TRAJECTORY_FIELDS.values())


def _catalog(payload: dict) -> dict:
    steps = {}
    for context in payload["steps"]:
        checks = context["oracleChecks"]
        findings = context["ruleFindings"]
        common = ({check["criterion"] for check in checks}
                  | {check["evidenceId"] for check in checks}
                  | {token for check in checks for token in check.get("evidence", [])}
                  | {finding["evidence"] for finding in findings})
        by_claim = {}
        for claim, prefix in (("target_execution", "target."),
                              ("post_action_reaction", "reaction."),
                              ("testcase_conformance", "flow.")):
            relevant = [check for check in checks if check["criterion"].startswith(prefix)]
            by_claim[claim] = ({check["criterion"] for check in relevant}
                               | {check["evidenceId"] for check in relevant}
                               | {token for check in relevant
                                  for token in check.get("evidence", [])}
                               | {f"nodeId={context['current']['nodeId']}"})
        steps[context["current"]["nodeId"]] = {
            "scores": {dimension: (STEP_FIELDS[dimension] | common
                                    | {context["fieldEvidence"][path]
                                       for path in STEP_FIELDS[dimension]
                                       if path in context["fieldEvidence"]})
                       for dimension in SCORE_DIMENSIONS},
            "claims": by_claim,
        }
    flow = payload["flowOracle"]["checks"]
    shared = ({check["criterion"] for check in flow}
              | {check["evidenceId"] for check in flow}
              | {token for check in flow for token in check.get("evidence", [])}
              | {finding["evidence"] for finding in payload["traceFindings"]})
    return {"steps": steps,
            "trajectory": {dimension: (TRAJECTORY_FIELDS[dimension] | shared
                                        | {payload["trajectoryEvidenceIds"][path]
                                           for path in TRAJECTORY_FIELDS[dimension]})
                           for dimension in TRAJECTORY_DIMENSIONS}}


def _citations(run: dict) -> list[dict]:
    rows = []
    for step in run.get("modelOutputs", []):
        node = step["nodeId"]
        for dimension, item in step.get("scoreEvidence", {}).items():
            for token in item.get("evidence", []):
                rows.append({"scope": "step-score", "nodeId": node,
                             "dimension": dimension, "token": token})
        for claim, item in step.get("claims", {}).items():
            for token in item.get("evidence", []):
                rows.append({"scope": "claim", "nodeId": node,
                             "dimension": claim, "token": token})
    trajectory = run.get("trajectory") or {}
    for dimension, item in trajectory.get("trajectoryEvidence", {}).items():
        for token in item.get("evidence", []):
            rows.append({"scope": "trajectory", "nodeId": None,
                         "dimension": dimension, "token": token})
    return rows


def _allowed(citation: dict, catalog: dict) -> set[str]:
    if citation["scope"] == "trajectory":
        return catalog["trajectory"].get(citation["dimension"], set())
    kind = "scores" if citation["scope"] == "step-score" else "claims"
    return catalog["steps"].get(citation["nodeId"], {}).get(kind, {}).get(
        citation["dimension"], set())


def _changed_truth_tokens(challenge: dict) -> set[str]:
    tokens = set()
    for change in challenge["expected"].get("changedChecks", []):
        key = change.get("key", "")
        if key.startswith("step:"):
            tokens.add(key.split(":", 2)[-1])
        tokens.update(token for token in change.get("evidence", []) if isinstance(token, str))
    return tokens


def _cites_truth(citations: set[str], truth_tokens: set[str]) -> bool:
    return any(token in citations
               or any(citation.startswith("oracle:") and f":{token}:" in citation
                      for citation in citations)
               for token in truth_tokens)


def audit_challenge_evidence(challenges: Iterable[dict], runs: Iterable[dict]) -> dict:
    """以 challenge 输入为权威，审计对应 run；缺失/失败 run 留在覆盖率分母。"""
    challenges = list(challenges)
    run_index = {run["id"]: run for run in runs}
    records = []
    for challenge in challenges:
        run = run_index.get(challenge["id"])
        if not run or run.get("status") != "success":
            records.append({"id": challenge["id"], "family": challenge["failureFamily"],
                            "status": "unavailable", "citations": 0, "grounded": 0,
                            "atomic": 0, "dimensionsCovered": 0})
            continue
        value = challenge["input"]
        payload = build_payload(value["trace"], goal="evidence-audit",
                                reference=value.get("reference"),
                                observations=value.get("observations"),
                                oracle_spec=value.get("oracleSpec"))
        catalog = _catalog(payload)
        citations = _citations(run)
        grounded = [row for row in citations if row["token"] in _allowed(row, catalog)]
        atomic = [row for row in grounded if row["token"] not in GENERIC_TOKENS]
        dimensions = {(row["scope"], row["nodeId"], row["dimension"])
                      for row in citations}
        grounded_dimensions = {(row["scope"], row["nodeId"], row["dimension"])
                               for row in grounded}
        records.append({"id": challenge["id"], "family": challenge["failureFamily"],
                        "status": "audited", "citations": len(citations),
                        "grounded": len(grounded), "atomic": len(atomic),
                        "dimensionsCovered": len(grounded_dimensions),
                        "dimensionsEmitted": len(dimensions),
                        "ungrounded": [row for row in citations if row not in grounded]})

    pair_rows = []
    for challenge in challenges:
        baseline_id = challenge.get("baselineId")
        if baseline_id is None:
            continue
        run, baseline = run_index.get(challenge["id"]), run_index.get(baseline_id)
        if not run or not baseline or run.get("status") != "success" or baseline.get("status") != "success":
            pair_rows.append({"id": challenge["id"], "family": challenge["failureFamily"],
                              "status": "unavailable", "evidenceChanged": None,
                              "defectCited": None})
            continue
        current_tokens = {row["token"] for row in _citations(run)}
        baseline_tokens = {row["token"] for row in _citations(baseline)}
        truth_tokens = _changed_truth_tokens(challenge)
        pair_rows.append({"id": challenge["id"], "family": challenge["failureFamily"],
                          "status": "audited",
                          "evidenceChanged": current_tokens != baseline_tokens,
                          "defectCited": _cites_truth(current_tokens, truth_tokens),
                          "changedTruthTokens": sorted(truth_tokens)})

    def ratio(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 4) if denominator else None

    citations = sum(row["citations"] for row in records)
    grounded = sum(row["grounded"] for row in records)
    atomic = sum(row["atomic"] for row in records)
    audited_pairs = [row for row in pair_rows if row["status"] == "audited"]
    families = {}
    for family in sorted({row["family"] for row in pair_rows}):
        group = [row for row in audited_pairs if row["family"] == family]
        families[family] = {"pairs": len(group),
                            "evidenceChangeRate": ratio(sum(row["evidenceChanged"] for row in group),
                                                        len(group)),
                            "defectCitationRate": ratio(sum(row["defectCited"] for row in group),
                                                        len(group))}
    audited_records = sum(row["status"] == "audited" for row in records)
    backlink_precision = ratio(grounded, citations)
    evidence_change_rate = ratio(sum(row["evidenceChanged"] for row in audited_pairs),
                                 len(audited_pairs))
    defect_citation_rate = ratio(sum(row["defectCited"] for row in audited_pairs),
                                 len(audited_pairs))
    gate_failures = []
    if audited_records != len(records):
        gate_failures.append("not-all-records-audited")
    if backlink_precision != 1.0:
        gate_failures.append("ungrounded-citation")
    if pair_rows and (len(audited_pairs) != len(pair_rows) or evidence_change_rate != 1.0):
        gate_failures.append("counterfactual-evidence-did-not-change")
    if pair_rows and (len(audited_pairs) != len(pair_rows) or defect_citation_rate != 1.0):
        gate_failures.append("counterfactual-defect-not-cited")
    return {"records": records, "pairs": pair_rows, "byFailureFamily": families,
            "contractGate": {"passed": not gate_failures, "failures": gate_failures,
                             "policy": "full coverage and exact deterministic evidence invariants"},
            "aggregate": {"records": len(records),
                          "auditedRecords": audited_records,
                          "citationCount": citations,
                          "backlinkPrecision": backlink_precision,
                          "atomicCitationRate": ratio(atomic, grounded),
                          "counterfactualPairs": len(audited_pairs),
                          "evidenceChangeRate": evidence_change_rate,
                          "defectCitationRate": defect_citation_rate,
                          "confidence": "deterministic-evidence-behavior-audit",
                          "interpretation": "citation diagnostics; not accuracy or probability"}}
