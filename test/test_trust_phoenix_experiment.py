import pytest

from trust.phoenix_experiment import run_versioned_experiment


class FakeExperiments:
    def __init__(self): self.kwargs = None
    def run_experiment(self, **kwargs):
        self.kwargs = kwargs
        return {"experiment": "ok"}


class FakeClient:
    def __init__(self): self.experiments = FakeExperiments()


class Dataset:
    version_id = "dataset-v3"


def _run(client=None, **kwargs):
    return run_versioned_experiment(
        dataset=Dataset(), task=lambda example: example, evaluators=[lambda output: True],
        model="judge-model", prompt_hash="abc123", dataset_version_id="dataset-v3",
        experiment_name="judge-v8", phoenix_client=client or FakeClient(), **kwargs)


def test_repeated_experiment_pins_all_provenance():
    client = FakeClient()
    assert _run(client=client, repetitions=3) == {"experiment": "ok"}
    kwargs = client.experiments.kwargs
    assert kwargs["repetitions"] == 3
    assert kwargs["experiment_metadata"]["traceEvalDatasetVersionId"] == "dataset-v3"
    assert kwargs["experiment_metadata"]["traceEvalModel"] == "judge-model"
    assert kwargs["experiment_metadata"]["interpretation"].endswith("not calibrated probability")


def test_single_repetition_is_only_allowed_for_dry_run():
    with pytest.raises(ValueError, match="至少"):
        _run(repetitions=1)
    assert _run(repetitions=1, dry_run=True) == {"experiment": "ok"}


def test_dataset_version_mismatch_and_metadata_override_fail_closed():
    with pytest.raises(ValueError, match="version"):
        run_versioned_experiment(
            dataset=Dataset(), task=lambda x: x, evaluators=[], model="m", prompt_hash="p",
            dataset_version_id="latest", experiment_name="x", phoenix_client=FakeClient())
    with pytest.raises(ValueError, match="覆盖"):
        _run(extra_metadata={"traceEvalModel": "other"})
