#!/usr/bin/env python3
"""统一入口：逐步规则评分 + 模型 judge + 保守聚合 + 可选参考轨迹匹配。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trust.hybrid import hybrid_evaluation
from trust.judge import SYSTEM_PROMPT, judge_trace
from trust.providers import OpenAIProvider
from trust.trajectory_match import MODES, match_trajectories
from trust.oracle import evaluate_test_case


def evaluate_trace(trace: dict, *, goal: str, provider, reference: dict | None = None,
                   observations: dict[str, dict] | None = None,
                   oracle_spec: dict | None = None,
                   system_prompt: str = SYSTEM_PROMPT,
                   cache_dir: str | Path | None = None) -> dict:
    judged = judge_trace(trace, goal=goal, provider=provider, reference=reference,
                         observations=observations,
                         oracle_spec=oracle_spec,
                         system_prompt=system_prompt,
                         cache_dir=cache_dir)
    oracle = evaluate_test_case(trace, reference or trace,
                                observations=observations,
                                oracle_spec=oracle_spec)
    result = hybrid_evaluation(trace, judged, oracle=oracle)
    result["judge"] = judged
    result["oracle"] = oracle
    result["match"] = ({mode: match_trajectories(trace, reference, mode)
                        for mode in sorted(MODES)} if reference else None)
    return result


def _load(path: str) -> dict:
    value = Path(path)
    value = value / "trace.json" if value.is_dir() else value
    return json.loads(value.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace")
    parser.add_argument("--goal", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reference")
    parser.add_argument("--cache-dir", default=".trust-judge-cache")
    parser.add_argument("--observations")
    parser.add_argument("--oracle-spec")
    parser.add_argument("--system-prompt-file")
    args = parser.parse_args(argv)
    result = evaluate_trace(_load(args.trace), goal=args.goal,
                            provider=OpenAIProvider(args.model),
                            reference=_load(args.reference) if args.reference else None,
                            observations=_load(args.observations) if args.observations else None,
                            oracle_spec=_load(args.oracle_spec) if args.oracle_spec else None,
                            system_prompt=(Path(args.system_prompt_file).read_text(encoding="utf-8")
                                           if args.system_prompt_file else SYSTEM_PROMPT),
                            cache_dir=args.cache_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
