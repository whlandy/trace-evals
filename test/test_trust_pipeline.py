import json

import pytest

from test_trust_judge import EvidenceAwareProvider
from test_trust_mutate import _good_trace
from trust.observation_mutate import complete_observations
from trust.pipeline import CASE_SCHEMA, evaluate_cases, read_case_manifest, validate_case


def _case(case="case-a"):
    trace = _good_trace()
    return {"schema": CASE_SCHEMA, "case": case, "goal": "保存策略", "trace": trace,
            "reference": trace, "observations": complete_observations(trace)}


def test_pipeline_runs_complete_architecture_and_routes_uncalibrated_output():
    report = evaluate_cases([_case()], provider=EvidenceAwareProvider())
    assert report["summary"] == {
        "cases": 1, "successful": 1, "errors": 0, "needsReview": 1,
        "reviewReasons": {"uncalibrated-score": 1},
        "interpretation": "evaluation routing; not model accuracy or probability"}
    evaluation = report["records"][0]["evaluation"]
    assert evaluation["provenance"]["rubricVersion"] == "trace-action-reaction-flow-v22"
    assert evaluation["oracle"]["summary"]["failed"] == 0
    assert all(step["claims"] for step in evaluation["steps"])


def test_pipeline_isolates_one_provider_error_and_keeps_other_cases():
    class FailsSecond(EvidenceAwareProvider):
        identity = "test:fails-second"
        def complete_json(self, **kwargs):
            if self.calls == 1:
                self.calls += 1
                raise RuntimeError("offline")
            return super().complete_json(**kwargs)

    report = evaluate_cases([_case("one"), _case("two")], provider=FailsSecond())
    assert report["summary"]["cases"] == 2
    assert report["summary"]["errors"] == 1
    assert report["summary"]["successful"] == 1
    assert report["records"][0]["status"] == "success"
    assert report["records"][1]["errorType"] == "RuntimeError"


def test_manifest_resolves_relative_json_inputs(tmp_path):
    trace = _good_trace()
    (tmp_path / "trace.json").write_text(json.dumps(trace), encoding="utf-8")
    manifest = tmp_path / "cases.jsonl"
    manifest.write_text(json.dumps({"schema": CASE_SCHEMA, "case": "relative",
                                    "goal": "保存策略", "trace": "trace.json"}) + "\n",
                        encoding="utf-8")
    rows = read_case_manifest(manifest)
    assert rows[0]["trace"]["$meta"]["attach"]["name"] == "good"


def test_case_contract_rejects_unknown_fields_and_duplicate_ids():
    bad = _case(); bad["magic"] = True
    with pytest.raises(ValueError, match="字段"):
        validate_case(bad)
    with pytest.raises(ValueError, match="唯一"):
        evaluate_cases([_case("same"), _case("same")], provider=EvidenceAwareProvider())


@pytest.mark.parametrize("bad", [None, [], "bad", {"case": []}, {"case": {}},
                                  {**_case(), "trace": None}])
def test_malformed_case_does_not_abort_valid_case(bad):
    provider = EvidenceAwareProvider()
    report = evaluate_cases([bad, _case()], provider=provider)
    assert report["summary"]["errors"] == 1
    assert report["summary"]["successful"] == 1
    assert report["records"][0]["inputIndex"] == 0
    assert provider.calls == 1


def test_cli_isolates_invalid_manifest_lines_and_returns_error(tmp_path, monkeypatch):
    from trust import pipeline

    manifest = tmp_path / "cases.jsonl"
    missing = {**_case("missing-file"), "trace": "missing.json"}
    manifest.write_text("\n".join([
        "{invalid", "[]", json.dumps({"case": []}), json.dumps(missing),
        json.dumps(_case("valid"))]), encoding="utf-8")
    destination = tmp_path / "report.json"
    provider = EvidenceAwareProvider()
    monkeypatch.setattr(pipeline, "OpenAIProvider", lambda *args, **kwargs: provider)
    status = pipeline.main([str(manifest), "--model", "test", "--out", str(destination),
                            "--cache-dir", str(tmp_path / "cache")])
    report = json.loads(destination.read_text(encoding="utf-8"))
    assert status == 1
    assert report["summary"]["errors"] == 4
    assert report["summary"]["successful"] == 1
    assert [row["source"] for row in report["records"][:4]] == [
        f"{manifest}:{number}" for number in range(1, 5)]
    assert provider.calls == 1
