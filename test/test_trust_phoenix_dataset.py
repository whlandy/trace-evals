import pytest

from test_trust_mutate import _good_trace
from trust.phoenix_dataset import (build_dataset_examples, compare_judges,
                                   upload_dataset)


def _gold(node="step_0001", verdict="pass", claim="target_execution"):
    return {"case": "case-a", "nodeId": node, "claim": claim, "verdict": verdict,
            "evidence": ["video:00:01"], "annotators": ["a", "b"],
            "votes": {verdict: 2}}


def _prediction(node, verdict, claim="target_execution"):
    return {"case": "case-a", "nodeId": node, "claim": claim, "verdict": verdict}


def test_dataset_keeps_input_reference_and_metadata_separate():
    rows = build_dataset_examples({"case-a": _good_trace()}, [_gold()], goal="保存策略")
    assert len(rows) == 1
    row = rows[0]
    assert row["input"]["claim"] == "target_execution"
    assert row["output"]["verdict"] == "pass"
    assert row["metadata"]["rubricVersion"]
    assert row["metadata"]["case"] == "case-a"
    assert len(row["example_id"]) == 24


def test_dataset_rejects_stale_gold_node():
    with pytest.raises(ValueError, match="nodeId"):
        build_dataset_examples({"case-a": _good_trace()}, [_gold("missing")], goal="x")


class FakeDatasets:
    def __init__(self): self.kwargs = None
    def create_dataset(self, **kwargs):
        self.kwargs = kwargs
        return type("Dataset", (), {"name": kwargs["name"], "version_id": "v1",
                                    "example_count": len(kwargs["dataframe"])})()


class FakeClient:
    def __init__(self): self.datasets = FakeDatasets()


def test_upload_uses_phoenix_inputs_outputs_metadata_contract():
    examples = build_dataset_examples({"case-a": _good_trace()}, [_gold()], goal="x")
    client = FakeClient()
    result = upload_dataset(examples, name="gold", phoenix_client=client,
                            dataframe_factory=lambda rows: rows)
    assert result == {"name": "gold", "versionId": "v1", "exampleCount": 1}
    assert client.datasets.kwargs["dataframe"][0]["verdict"] == "pass"
    assert client.datasets.kwargs["example_id_key"] == "example_id"
    assert "oracleSpecHash" in client.datasets.kwargs["metadata_keys"]


def test_oracle_spec_is_versioned_and_visible_in_dataset_input():
    spec = {"schema": "trace-eval.oracle-spec/v1", "nodes": {
        "step_0003": {"persistence": {"method": "reload",
                                        "expected": {"enabled": True}}}}}
    rows = build_dataset_examples({"case-a": _good_trace()},
                                  [_gold("step_0003", claim="post_action_reaction")],
                                  goal="保存策略", oracle_specs={"case-a": spec})
    assert rows[0]["input"]["nodeOracleSpec"] == spec["nodes"]["step_0003"]
    assert len(rows[0]["metadata"]["oracleSpecHash"]) == 64
    checks = rows[0]["input"]["stepContext"]["oracleChecks"]
    assert next(x for x in checks if x["criterion"] == "reaction.persistent_state")["verdict"] == "unknown"


def test_dataset_rejects_oracle_spec_for_unknown_case():
    with pytest.raises(ValueError, match="case"):
        build_dataset_examples({"case-a": _good_trace()}, [_gold()], goal="x",
                               oracle_specs={"case-b": {}})


def test_regression_gate_is_per_claim_and_fails_on_abstention():
    gold = [_gold("step_0001", "pass"), _gold("step_0002", "fail")]
    baseline = [_prediction("step_0001", "pass"), _prediction("step_0002", "fail")]
    candidate = [_prediction("step_0001", "pass"), _prediction("step_0002", "unknown")]
    report = compare_judges(baseline, candidate, gold)
    assert report["passed"] is False
    coverage = next(item for item in report["checks"]
                    if item["claim"] == "target_execution" and item["metric"] == "coverage")
    assert coverage["delta"] == -0.5


def test_missing_claim_data_fails_closed_in_regression_gate():
    report = compare_judges([], [], [_gold()])
    assert report["passed"] is False
    assert any(item["delta"] is None for item in report["checks"])
