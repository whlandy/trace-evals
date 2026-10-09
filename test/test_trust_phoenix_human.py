import pytest

from trust.phoenix_human import build_human_annotation_plan, log_human_annotations


def _label(annotator="alice"):
    return {"case": "case-a", "nodeId": "step_0001", "claim": "target_execution",
            "verdict": "pass", "annotator": annotator, "evidence": ["video:00:01"]}


def test_human_plan_preserves_each_annotator_and_uses_idempotent_identifier():
    plan = build_human_annotation_plan([_label("alice"), _label("bob")],
                                       {"case-a": {"step_0001": "abc123"}})
    assert len(plan) == 2
    assert all(row["annotator_kind"] == "HUMAN" for row in plan)
    assert len({row["identifier"] for row in plan}) == 2
    assert plan[0]["metadata"]["interpretation"].endswith("not probability")


def test_human_plan_rejects_missing_span_and_duplicate_annotator_label():
    with pytest.raises(ValueError, match="span"):
        build_human_annotation_plan([_label()], {})
    with pytest.raises(ValueError, match="重复"):
        build_human_annotation_plan([_label(), _label()],
                                    {"case-a": {"step_0001": "abc123"}})


class FakeSpans:
    def __init__(self): self.kwargs = None
    def log_span_annotations(self, **kwargs): self.kwargs = kwargs


class FakeClient:
    def __init__(self): self.spans = FakeSpans()


def test_human_annotations_are_logged_synchronously():
    client = FakeClient()
    plan = build_human_annotation_plan([_label()],
                                       {"case-a": {"step_0001": "abc123"}})
    result = log_human_annotations(plan, phoenix_client=client)
    assert result["annotatorKind"] == "HUMAN"
    assert client.spans.kwargs["sync"] is True
