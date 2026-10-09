from contextlib import contextmanager

from test_trust_mutate import _good_trace
from trust.phoenix_export import build_phoenix_plan, export_plan, verify_export
from trust.hybrid import hybrid_evaluation
from trust.judge import judge_trace
from test_trust_judge import EvidenceAwareProvider


def test_plan_maps_every_trace_step_and_all_five_dimensions():
    plan = build_phoenix_plan(_good_trace())
    assert len(plan["children"]) == 4
    for child in plan["children"]:
        names = {item["name"] for item in child["annotations"]}
        assert "trace_eval.step_overall" in names
        dimensions = {name for name in names if name.startswith("trace_eval.")
                      and not name.startswith("trace_eval.claim.")
                      and name != "trace_eval.step_overall"}
        assert len(dimensions) == 5
        assert child["attributes"]["trace_eval.confidence"] == "deterministic"


def test_annotation_metadata_explicitly_denies_probability_semantics():
    plan = build_phoenix_plan(_good_trace())
    annotations = [a for child in plan["children"] for a in child["annotations"]]
    assert all(a["metadata"]["interpretation"] == "ordering-only-not-a-probability"
               for a in annotations)


def test_dimension_annotations_include_score_specific_evidence():
    plan = build_phoenix_plan(_good_trace())
    annotation = next(a for a in plan["children"][0]["annotations"]
                      if a["name"] == "trace_eval.replay_safety")
    assert "evidence:" in annotation["result"]["explanation"]


def test_hybrid_root_exports_trajectory_dimension_evidence():
    trace = _good_trace()
    judged = judge_trace(trace, goal="保存策略", provider=EvidenceAwareProvider())
    plan = build_phoenix_plan(trace, hybrid_evaluation(trace, judged))
    annotation = next(a for a in plan["annotations"]
                      if a["name"] == "trace_eval.trajectory.ordering")
    assert "referenceSteps" in annotation["result"]["explanation"]


def test_hybrid_export_preserves_distinct_code_and_llm_annotation_kinds():
    trace = _good_trace()
    judged = judge_trace(trace, goal="保存策略", provider=EvidenceAwareProvider())
    plan = build_phoenix_plan(trace, hybrid_evaluation(trace, judged))
    assert plan["name"] == "trace.eval.good"
    score_annotations = [a for a in plan["children"][0]["annotations"]
                         if not a["name"].startswith("trace_eval.oracle.")
                         and not a["name"].startswith("trace_eval.claim.")]
    assert all(annotation["annotator_kind"] == "CODE" for annotation in score_annotations)
    claims = [a for a in plan["children"][0]["annotations"]
              if a["name"].startswith("trace_eval.claim.")]
    assert all(annotation["annotator_kind"] == "LLM" for annotation in claims)
    assert {a["result"]["label"] for a in claims} >= {"unknown"}
    assert claims[0]["identifier"].endswith(":model")


def test_every_hybrid_annotation_identifier_is_rubric_versioned():
    trace = _good_trace()
    judged = judge_trace(trace, goal="保存策略", provider=EvidenceAwareProvider())
    plan = build_phoenix_plan(trace, hybrid_evaluation(trace, judged))
    annotations = [*plan["annotations"],
                   *(item for child in plan["children"] for item in child["annotations"])]
    rubric = judged["provenance"]["rubricVersion"]
    assert annotations
    assert all(annotation["identifier"].startswith(f"trace-eval:{rubric}:")
               for annotation in annotations)


def test_unified_result_exports_atomic_oracle_checks():
    from trust.evaluate import evaluate_trace
    trace = _good_trace()
    evaluated = evaluate_trace(trace, goal="保存策略", provider=EvidenceAwareProvider())
    plan = build_phoenix_plan(trace, evaluated)
    names = {a["name"] for child in plan["children"] for a in child["annotations"]}
    assert "trace_eval.oracle.target.resolved" in names
    assert any(a["name"].startswith("trace_eval.oracle_summary.")
               for a in plan["annotations"])


class FakeSpan:
    current = 0
    def __init__(self):
        FakeSpan.current += 1
        self.value = FakeSpan.current
    def get_span_context(self):
        return type("Context", (), {"span_id": self.value})()


class FakeTracer:
    @contextmanager
    def start_as_current_span(self, name, attributes):
        yield FakeSpan()


class FakeProvider:
    def __init__(self): self.flushed = False
    def get_tracer(self, name): return FakeTracer()
    def force_flush(self): self.flushed = True


class FakeSpans:
    def __init__(self): self.logged = None
    def log_span_annotations(self, **kwargs): self.logged = kwargs


class FakeClient:
    def __init__(self): self.spans = FakeSpans()


def test_export_flushes_spans_before_logging_annotations():
    provider, client = FakeProvider(), FakeClient()
    result = export_plan(build_phoenix_plan(_good_trace()),
                         tracer_provider=provider, phoenix_client=client)
    assert provider.flushed is True
    assert result["annotationCount"] == len(client.spans.logged["span_annotations"])
    assert client.spans.logged["sync"] is True
    assert len(result["spanIds"]) == 5


class ReadbackSpans:
    def __init__(self, spans, annotations):
        self._spans, self._annotations = spans, annotations
    def get_spans(self, **kwargs): return self._spans
    def get_span_annotations(self, **kwargs): return self._annotations


def _readback(plan, exported, *, drop_last=False):
    spans = [{"context": {"span_id": value}} for value in exported["spanIds"].values()]
    annotations = []
    for item in [plan, *plan["children"]]:
        annotations.extend({**annotation, "span_id": exported["spanIds"][item["key"]]}
                           for annotation in item["annotations"])
    if drop_last:
        annotations.pop()
    client = type("ReadbackClient", (), {})()
    client.spans = ReadbackSpans(spans, annotations)
    return client


def test_verify_export_requires_exact_span_and_annotation_readback():
    plan = build_phoenix_plan(_good_trace())
    exported = {"project": "trace-eval", "baseUrl": "http://phoenix",
                "spanIds": {plan["key"]: "root", **{child["key"]: f"s{i}"
                                                       for i, child in enumerate(plan["children"])}},
                "annotationCount": sum(len(item["annotations"])
                                       for item in [plan, *plan["children"]])}
    passed = verify_export(plan, exported, phoenix_client=_readback(plan, exported))
    assert passed["passed"] is True
    assert len(passed["proofHash"]) == 64
    failed = verify_export(plan, exported,
                           phoenix_client=_readback(plan, exported, drop_last=True))
    assert failed["passed"] is False
    assert len(failed["missingAnnotations"]) == 1
