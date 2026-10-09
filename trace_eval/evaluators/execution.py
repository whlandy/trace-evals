"""Adapter evaluator：把 `trust.maa_execution.evaluate()` 转成统一 EvaluatorResult[]。

Round 1 只做协议适配。转换语义：

- `EvaluationError`（digest mismatch、incomplete Golden、schema 不符……）
  → 单条 fail 结果，failureCode=EVALUATION_REJECTED，报错文本原样进 evidence。
  完整性被拒是**硬失败**，不是 inconclusive：拿错 Golden 的执行结果不可信；
- 正常报告拆两条：
  - execution.maa.task：taskSuccess → pass/fail，score 用原报告分值
    （calibration=deterministic），失败时 failedNodeIds 进 nodeId/evidence；
  - execution.maa.metrics：四个比率指标（completion/action/order/efficiency）
    期望 1.0；averageVisualMatchScore 是质量读数不是通过判据，
    只进 observed 不判 verdict（0.92 的视觉置信度是正常值，不是失败）。
"""

from __future__ import annotations

from trace_eval.contracts import EvaluatorResult

EVALUATOR_NAME = "maa-execution"
EVALUATOR_VERSION = "1.0.0"
_EVALUATOR = {"name": EVALUATOR_NAME, "version": EVALUATOR_VERSION}

_RATE_METRICS = ("stepCompletionRate", "actionAccuracy",
                 "trajectoryOrderRate", "trajectoryEfficiency")


class MaaExecutionEvaluator:
    key = "execution.maa"
    version = EVALUATOR_VERSION
    scope = "run"

    def evaluate(self, golden: dict, execution: dict) -> list[EvaluatorResult]:
        from trust.maa_execution import EvaluationError, evaluate
        try:
            report = evaluate(golden, execution)
        except EvaluationError as error:
            return [self._rejected_result(str(error))]
        return [self._task_result(report), self._metrics_result(report)]

    def _rejected_result(self, message: str) -> EvaluatorResult:
        return EvaluatorResult(
            key="execution.maa.integrity",
            scope=self.scope,
            verdict="fail",
            failure_code="EVALUATION_REJECTED",
            evidence=[message],
            comment=message,
            evaluator=dict(_EVALUATOR),
        )

    def _task_result(self, report: dict) -> EvaluatorResult:
        verdict = "pass" if report["taskSuccess"] else "fail"
        result = EvaluatorResult(
            key="execution.maa.task",
            scope=self.scope,
            verdict=verdict,
            score=report["score"],
            calibration="deterministic",
            observed={"taskSuccess": report["taskSuccess"],
                      "stepCompletionRate": report["stepCompletionRate"],
                      "actionAccuracy": report["actionAccuracy"],
                      "trajectoryOrderRate": report["trajectoryOrderRate"],
                      "trajectoryEfficiency": report["trajectoryEfficiency"],
                      "averageVisualMatchScore": report["averageVisualMatchScore"],
                      "expectedStepCount": report["expectedStepCount"],
                      "observedStepCount": report["observedStepCount"],
                      "failedNodeIds": report["failedNodeIds"]},
            evaluator=dict(_EVALUATOR),
        )
        if verdict == "fail":
            failed = list(report.get("failedNodeIds") or [])
            result.failure_code = "TASK_NOT_SUCCESSFUL"
            result.evidence = (
                [f"taskSuccess=false; failed nodes={failed}"] if failed
                else ["taskSuccess=false（路径或完整性未达预期，无单节点归因）"])
            if len(failed) == 1:
                result.node_id = failed[0]
        return result

    def _metrics_result(self, report: dict) -> EvaluatorResult:
        observed = {metric: report[metric] for metric in _RATE_METRICS}
        observed["averageVisualMatchScore"] = report["averageVisualMatchScore"]
        below = [metric for metric in _RATE_METRICS if report[metric] != 1.0]
        result = EvaluatorResult(
            key="execution.maa.metrics",
            scope=self.scope,
            verdict="fail" if below else "pass",
            calibration="deterministic",
            expected={metric: 1.0 for metric in _RATE_METRICS},
            observed=observed,
            comment=("averageVisualMatchScore 是质量读数，不参与 verdict"
                     if report["averageVisualMatchScore"] is not None else
                     "无视觉读数（未提供 matchScore）"),
            evaluator=dict(_EVALUATOR),
        )
        if below:
            result.failure_code = "METRIC_BELOW_EXPECTED"
            result.evidence = [f"{metric}={report[metric]} < 1.0" for metric in below]
        return result
