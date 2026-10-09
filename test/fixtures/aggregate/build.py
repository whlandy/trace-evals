#!/usr/bin/env python3
"""Round 6 快照 fixture 生成器（一次性运行，输出冻结进仓库）。

历史 Experiment 快照测试（设计 Round 6 验收第 6 条）的素材：
三个手写的历史 Experiment 目录 + 当前代码算出的 summary/comparison
冻结文件。**之后任何一轮**只要聚合语义变化，重算结果就会与冻结
文件不符 → 快照测试失败 —— 历史结果不会被当前代码「重新解释」。

三个 Experiment（digest D1 是同一 Dataset 版本的两次实验，D2 是
另一版本）：

baseline：  case-a pass | case-b fail(ORACLE_API_STATE_WRONG)
            | case-c 2×pass 100（stable-green）| case-d invalid
candidate： case-a fail（新增失败）| case-b pass（修复）
            | case-c 2×pass 100/90（drifting）| case-d invalid
other：     case-a pass | case-e pass（Dataset 不同：加 case-e）
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))

from trace_eval.aggregate import (  # noqa: E402
    aggregate_experiment, compare_experiments,
)
from trust.stable_json import dumps_stable  # noqa: E402

D1 = "sha256:baseline-dataset-d1"
D2 = "sha256:other-dataset-d2"
TS = "2026-10-09T00:00:00Z"

CASES = {
    "case-a": {"executor": "web", "tags": ["contract"], "risk": "high"},
    "case-b": {"executor": "maa", "tags": ["write"], "risk": "high"},
    "case-c": {"executor": "web", "tags": ["contract", "ui"], "risk": None},
    "case-d": {"executor": "web", "tags": [], "risk": "low"},
    "case-e": {"executor": "maa", "tags": ["write"], "risk": "high"},
}


def run_json(experiment_id: str, case: str, trial: int, status: str,
             seq: int) -> dict:
    return {
        "schema": "trace-evals.run/v1",
        "runId": f"{case}__v1__t{trial:02d}__{seq:08x}",
        "experimentId": experiment_id, "caseId": case,
        "variantId": "v1", "trial": trial, "status": status,
        "startedAt": TS, "finishedAt": TS, "artifacts": {},
        "environment": {"gitSha": "0" * 40, "datasetDigest": D1,
                        "variantId": "v1", "trial": trial,
                        "adapter": "replay", "timeout": 30.0,
                        "statePolicy": "reset", "startedAt": TS},
    }


def result(key: str, verdict: str, score=None, code=None, evidence=None,
           comment=None) -> dict:
    r = {
        "schema": "trace-evals.result/v1", "key": key, "scope": "case",
        "verdict": verdict, "score": score,
        "calibration": "deterministic", "failureCode": code,
        "nodeId": None, "expected": None, "observed": None,
        "evidence": evidence, "comment": comment,
        "evaluator": {"name": "fixture-evaluator", "version": "1.0"},
    }
    return r


def write_experiment(base: Path, exp_id: str, digest: str,
                     runs: list[tuple[str, int, str, list[dict]]]) -> None:
    (base / exp_id / "runs").mkdir(parents=True, exist_ok=True)
    case_ids = sorted({r[0] for r in runs})
    (base / exp_id / "experiment.json").write_text(json.dumps({
        "schema": "trace-evals.experiment/v1",
        "id": exp_id, "datasetDigest": digest,
        "config": {"trials": max(t for _, t, _, _ in runs),
                  "cases": [dict(CASES[c], id=c) for c in case_ids]},
        "startedAt": TS,
    }, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    seq = 1
    for case, trial, status, results in runs:
        run_dir = base / exp_id / "runs" / \
            f"{case}__v1__t{trial:02d}__{seq:08x}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(
            json.dumps(run_json(exp_id, case, trial, status, seq),
                        ensure_ascii=False, sort_keys=True),
            encoding="utf-8")
        if results:
            (run_dir / "results.jsonl").write_text(
                "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True)
                          for r in results) + "\n", encoding="utf-8")
        seq += 1


def freeze(base: Path, name: str, payload: dict) -> None:
    (base / f"{name}.json").write_text(
        dumps_stable(payload) + "\n", encoding="utf-8")


def main() -> None:
    base = HERE
    # 清掉旧 fixture（重新冻结）
    for d in ("experiment-baseline", "experiment-candidate", "experiment-other"):
        target = base / d
        if target.exists():
            import shutil
            shutil.rmtree(target)
    for f in base.glob("*.json"):
        f.unlink()

    write_experiment(base, "experiment-baseline", D1, [
        ("case-a", 1, "completed",
         [result("execution.maa.task", "pass", score=100)]),
        ("case-b", 1, "completed",
         [result("execution.maa.task", "pass", score=80),
          result("oracle.api-json.backend-state", "fail",
                 code="ORACLE_API_STATE_WRONG",
                 evidence=["artifacts/response.json"],
                 comment="expected enabled, observed locked")]),
        ("case-c", 1, "completed", [result("execution.maa.task", "pass",
                                          score=100)]),
        ("case-c", 2, "completed", [result("execution.maa.task", "pass",
                                          score=100)]),
        ("case-d", 1, "invalid", []),
    ])
    write_experiment(base, "experiment-candidate", D1, [
        ("case-a", 1, "completed",
         [result("execution.maa.task", "pass", score=100),
          result("oracle.api-json.backend-state", "fail",
                 code="ORACLE_API_STATE_WRONG",
                 evidence=["artifacts/response.json"],
                 comment="expected enabled, observed locked")]),
        ("case-b", 1, "completed", [result("execution.maa.task", "pass",
                                          score=80)]),
        ("case-c", 1, "completed", [result("execution.maa.task", "pass",
                                          score=100)]),
        ("case-c", 2, "completed", [result("execution.maa.task", "pass",
                                          score=90)]),
        ("case-d", 1, "invalid", []),
    ])
    write_experiment(base, "experiment-other", D2, [
        ("case-a", 1, "completed", [result("execution.maa.task", "pass",
                                          score=100)]),
        ("case-e", 1, "completed", [result("execution.maa.task", "pass",
                                          score=100)]),
    ])

    freeze(base, "summary-baseline",
           aggregate_experiment(base / "experiment-baseline"))
    freeze(base, "summary-candidate",
           aggregate_experiment(base / "experiment-candidate"))
    freeze(base, "comparison-baseline-candidate",
           compare_experiments(base / "experiment-baseline",
                               base / "experiment-candidate"))
    freeze(base, "comparison-baseline-other",
           compare_experiments(base / "experiment-baseline",
                               base / "experiment-other"))
    print("fixture 已冻结：", sorted(p.name for p in base.iterdir()))


if __name__ == "__main__":
    main()
