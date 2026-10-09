import copy

import pytest

from test_trust_judge import EvidenceAwareProvider
from test_trust_mutate import _good_trace
from trust.challenge_set import build_challenge_set
from trust.judge import RUBRIC_VERSION, SYSTEM_PROMPT
from trust.prompt_robustness import (REQUIRED_INVARIANTS, analyze_prompt_robustness,
                                     run_prompt_variants, validate_prompt_bundle)


def _bundle():
    return {"schema": "trace-eval.prompt-variants/v1", "rubricVersion": RUBRIC_VERSION,
            "variants": [
                {"id": "canonical", "prompt": SYSTEM_PROMPT,
                 "certifiedEquivalent": True, "invariants": sorted(REQUIRED_INVARIANTS)},
                {"id": "reformatted", "prompt": SYSTEM_PROMPT + "\n",
                 "certifiedEquivalent": True, "invariants": sorted(REQUIRED_INVARIANTS)},
            ]}


def test_equivalent_prompt_bundle_runs_with_distinct_cache_identity(tmp_path):
    challenges = build_challenge_set(_good_trace(), case="good")[:2]
    provider = EvidenceAwareProvider()
    reports = run_prompt_variants(challenges, _bundle(), provider=provider,
                                  goal="保存策略", cache_dir=tmp_path)
    assert provider.calls == 4
    assert reports[0]["promptId"] != reports[1]["promptId"]
    assert reports[0]["promptHash"] != reports[1]["promptHash"]
    result = analyze_prompt_robustness(reports)
    assert result["aggregate"]["claimFlipRate"] == 0
    assert result["aggregate"]["robust"] is True


def test_score_sensitivity_is_visible_even_when_hard_claims_stay_valid(tmp_path):
    class PromptSensitive(EvidenceAwareProvider):
        identity = "test:prompt-sensitive"
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            if kwargs["system"].endswith("\n"):
                for step in result["steps"]:
                    step["scores"] = {key: 0.5 for key in step["scores"]}
            return result
    challenges = build_challenge_set(_good_trace(), case="good")[:2]
    reports = run_prompt_variants(challenges, _bundle(), provider=PromptSensitive(),
                                  goal="保存策略", cache_dir=tmp_path)
    result = analyze_prompt_robustness(reports, score_range_threshold=0.1)
    assert result["aggregate"]["claimFlipRate"] == 0
    assert result["aggregate"]["scoreRangeExceedances"] == 2
    assert result["aggregate"]["robust"] is False


def test_bundle_requires_human_certification_and_all_invariants():
    bad = _bundle(); bad["variants"][1]["certifiedEquivalent"] = False
    with pytest.raises(ValueError, match="等价性"):
        validate_prompt_bundle(bad)
    bad = _bundle(); bad["variants"][1]["invariants"].pop()
    with pytest.raises(ValueError, match="invariants"):
        validate_prompt_bundle(bad)


def test_analysis_rejects_different_challenge_sets():
    challenges = build_challenge_set(_good_trace(), case="good")[:2]
    reports = run_prompt_variants(challenges, _bundle(), provider=EvidenceAwareProvider(),
                                  goal="保存策略")
    bad = copy.deepcopy(reports); bad[1]["runs"].pop()
    with pytest.raises(ValueError, match="IDs"):
        analyze_prompt_robustness(bad)
