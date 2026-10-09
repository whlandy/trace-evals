#!/usr/bin/env python3
"""生成按失败形态分层、带确定性真值的 trace-eval challenge set。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from trust.mutate import MUTATIONS, mutate
from trust.observation_mutate import (OBSERVATION_MUTATIONS, complete_observations,
                                      mutate_observation)
from trust.oracle import evaluate_test_case
from trust.step_score import score_steps

SCHEMA = "trace-eval.challenge-set/v1"

OBSERVATION_FAMILIES = {
    "miss_target": "grounding", "ambiguous_target": "grounding",
    "edge_hit": "grounding-margin",
    "target_not_resolved": "grounding", "hidden_target": "actionability",
    "disabled_target": "actionability", "obscured_target": "actionability",
    "wrong_semantic_target": "semantic-target", "action_failed": "action-execution",
    "missing_network_reaction": "reaction-missing", "wrong_network_status": "reaction-wrong",
    "uncorrelated_network_reaction": "reaction-causality",
    "next_step_not_ready": "flow-runtime-transition",
    "failed_assertion": "outcome-assertion", "failed_downstream_assertion": "outcome-closure",
}

PERSISTENCE_MUTATIONS = ("wrong-value", "wrong-method", "too-late", "no-evidence",
                         "unknown-source")


def _id(case: str, layer: str, mutation: str) -> str:
    value = hashlib.sha256(f"{case}\0{layer}\0{mutation}".encode()).hexdigest()[:20]
    return f"{case}:{layer}:{mutation}:{value}"


def _claim(checks: list[dict], prefix: str) -> str:
    verdicts = [item["verdict"] for item in checks if item["criterion"].startswith(prefix)]
    if "fail" in verdicts:
        return "fail"
    if "unknown" in verdicts:
        return "unknown"
    if "pass" in verdicts:
        return "pass"
    return "not_applicable"


def _expected(oracle: dict) -> dict:
    step_claims = []
    for step in oracle["steps"]:
        checks = step["checks"]
        step_claims.append({
            "nodeId": step["actualNodeId"] or step["referenceNodeId"],
            "target_execution": _claim(checks, "target."),
            "post_action_reaction": _claim(checks, "reaction."),
            "testcase_conformance": _claim(checks, "flow."),
        })
    return {"summary": oracle["summary"], "stepClaims": step_claims,
            "flowVerdicts": {item["criterion"]: item["verdict"]
                             for item in oracle["flowChecks"]}}


def _check_index(oracle: dict) -> dict[str, dict]:
    result = {}
    for step in oracle["steps"]:
        node = step["actualNodeId"] or step["referenceNodeId"]
        for item in step["checks"]:
            result[f"step:{node}:{item['criterion']}"] = item
    for item in oracle["flowChecks"]:
        result[f"flow:{item['criterion']}"] = item
    return result


def _changed_checks(base: dict, changed: dict) -> list[dict]:
    before, after = _check_index(base), _check_index(changed)
    rows = []
    for key in sorted(set(before) | set(after)):
        left, right = before.get(key), after.get(key)
        if (left or {}).get("verdict") != (right or {}).get("verdict"):
            rows.append({"key": key, "before": (left or {}).get("verdict"),
                         "after": (right or {}).get("verdict"),
                         "evidence": (right or {}).get("evidence", []),
                         "reason": (right or {}).get("reason")})
    return rows


def _rule_expected(scored: dict) -> dict:
    return {"aggregate": scored["aggregate"],
            "steps": [{"nodeId": step["nodeId"], "scores": step["scores"],
                       "scoreEvidence": step["scoreEvidence"],
                       "overall": step["overall"], "label": step["label"],
                       "findings": step["findings"]} for step in scored["steps"]],
            "traceFindings": scored["traceFindings"]}


def _changed_rules(base: dict, changed: dict) -> list[dict]:
    before = {step["nodeId"]: step for step in base["steps"]}
    after = {step["nodeId"]: step for step in changed["steps"]}
    rows = []
    for node_id in sorted(set(before) | set(after)):
        left, right = before.get(node_id), after.get(node_id)
        if left == right:
            continue
        new_findings = [item for item in (right or {}).get("findings", [])
                        if item not in (left or {}).get("findings", [])]
        rows.append({"key": f"rule:{node_id}",
                     "before": (left or {}).get("overall"),
                     "after": (right or {}).get("overall"),
                     "evidence": [item["evidence"] for item in new_findings]
                     or [(right or {}).get("reason", "rule score changed")],
                     "reason": "deterministic rule evaluation changed"})
    return rows


def _record(*, case: str, layer: str, mutation: str, family: str, description: str,
            trace: dict, reference: dict, observations: dict, oracle: dict,
            baseline_id: str | None, oracle_spec: dict | None = None,
            base_oracle: dict | None = None, base_rules: dict | None = None) -> dict:
    rules = score_steps(trace)
    changes = (_changed_checks(base_oracle, oracle) if base_oracle is not None else [])
    if base_rules is not None:
        changes += _changed_rules(base_rules, rules)
    return {"schema": SCHEMA, "id": _id(case, layer, mutation), "case": case,
            "layer": layer, "mutation": mutation, "failureFamily": family,
            "description": description, "baselineId": baseline_id,
            "input": {"trace": trace, "reference": reference,
                      "observations": observations, "oracleSpec": oracle_spec},
            "expected": {**_expected(oracle), "rules": _rule_expected(rules),
                         "changedChecks": changes},
            "confidence": "deterministic-synthetic-counterfactual",
            "interpretation": "challenge truth, not a calibrated success probability"}


def build_challenge_set(trace: dict, *, case: str = "reference") -> list[dict]:
    records = []
    observations = complete_observations(trace)
    base_oracle = evaluate_test_case(trace, trace, observations=observations)
    base_rules = score_steps(trace)
    runtime_base_id = _id(case, "runtime", "clean")
    records.append(_record(case=case, layer="runtime", mutation="clean", family="control",
                           description="完整运行观测基线", trace=trace, reference=trace,
                           observations=observations, oracle=base_oracle, baseline_id=None))
    for name in OBSERVATION_MUTATIONS:
        changed, description = mutate_observation(trace, observations, name)
        oracle = evaluate_test_case(trace, trace, observations=changed)
        records.append(_record(
            case=case, layer="runtime", mutation=name, family=OBSERVATION_FAMILIES[name],
            description=description, trace=trace, reference=trace, observations=changed,
            oracle=oracle, baseline_id=runtime_base_id, base_oracle=base_oracle))

    for name in sorted(MUTATIONS):
        changed = mutate(trace, name)
        if changed is None:
            continue
        mutated, description = changed
        mutated_observations = complete_observations(mutated)
        oracle = evaluate_test_case(mutated, trace, observations=mutated_observations)
        records.append(_record(
            case=case, layer="trajectory", mutation=name, family="testcase-conformance",
            description=description, trace=mutated, reference=trace,
            observations=mutated_observations, oracle=oracle,
            baseline_id=runtime_base_id, base_oracle=base_oracle, base_rules=base_rules))

    spec = {"schema": "trace-eval.oracle-spec/v1", "nodes": {
        "step_0003": {"persistence": {"method": "reload",
                                        "expected": {"enabled": True},
                                        "maxDelayMs": 5000}}}}
    if "step_0003" in trace:
        persistent = complete_observations(trace)
        persistent["step_0003"]["reactions"]["persistence"] = {
            "method": "reload", "passed": True, "observed": {"enabled": True},
            "delayMs": 100, "evidence": ["synthetic:after-reload"],
            "evidenceSource": "test-fixture"}
        persistent_base = evaluate_test_case(trace, trace, observations=persistent,
                                             oracle_spec=spec)
        persistent_base_id = _id(case, "persistence", "clean")
        records.append(_record(
            case=case, layer="persistence", mutation="clean", family="control",
            description="刷新后状态仍符合预期", trace=trace, reference=trace,
            observations=persistent, oracle_spec=spec, oracle=persistent_base,
            baseline_id=None))
        for name in PERSISTENCE_MUTATIONS:
            changed = copy.deepcopy(persistent)
            value = changed["step_0003"]["reactions"]["persistence"]
            if name == "wrong-value": value["observed"] = {"enabled": False}
            elif name == "wrong-method": value["method"] = "api"
            elif name == "too-late": value["delayMs"] = 6000
            elif name == "no-evidence": value["evidence"] = []
            else: value["evidenceSource"] = "model-vibes"
            oracle = evaluate_test_case(trace, trace, observations=changed, oracle_spec=spec)
            records.append(_record(
                case=case, layer="persistence", mutation=name, family="persistence",
                description=f"持久状态反事实：{name}", trace=trace, reference=trace,
                observations=changed, oracle_spec=spec, oracle=oracle,
                baseline_id=persistent_base_id, base_oracle=persistent_base))
    return records


def validate_challenge_set(records: list[dict]) -> dict:
    ids = [row["id"] for row in records]
    if len(ids) != len(set(ids)):
        raise ValueError("challenge id 不唯一")
    by_id = {row["id"]: row for row in records}
    failures = []
    for row in records:
        if row["baselineId"] is None:
            continue
        baseline = by_id.get(row["baselineId"])
        if baseline is None:
            failures.append({"id": row["id"], "reason": "missing-baseline"})
            continue
        before = baseline["expected"]["summary"]["evidenceAdjustedOrderingScore"]
        after = row["expected"]["summary"]["evidenceAdjustedOrderingScore"]
        rule_before = baseline["expected"]["rules"]["aggregate"]["bottomKMean"]
        rule_after = row["expected"]["rules"]["aggregate"]["bottomKMean"]
        oracle_decreased = after is not None and before is not None and after < before
        rules_decreased = (rule_after is not None and rule_before is not None
                           and rule_after < rule_before)
        if not oracle_decreased and not rules_decreased:
            failures.append({"id": row["id"], "reason": "score-did-not-decrease",
                             "oracleBefore": before, "oracleAfter": after,
                             "rulesBefore": rule_before, "rulesAfter": rule_after})
        if not row["expected"]["changedChecks"]:
            failures.append({"id": row["id"], "reason": "no-changed-check"})
    return {"valid": not failures, "records": len(records),
            "controls": sum(row["baselineId"] is None for row in records),
            "counterfactuals": sum(row["baselineId"] is not None for row in records),
            "families": sorted({row["failureFamily"] for row in records}),
            "failures": failures}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace")
    parser.add_argument("--case", default="reference")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    path = Path(args.trace)
    path = path / "trace.json" if path.is_dir() else path
    trace = json.loads(path.read_text(encoding="utf-8"))
    records = build_challenge_set(trace, case=args.case)
    report = validate_challenge_set(records)
    if not report["valid"]:
        raise SystemExit(json.dumps(report, ensure_ascii=False, indent=2))
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
