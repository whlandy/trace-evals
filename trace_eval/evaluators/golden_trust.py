"""Adapter evaluator：把既有 `trust.audit.audit()` 转成统一 EvaluatorResult[]。

Round 1 只做协议适配（设计 §15 第 2 条），不移动评分逻辑。

转换语义：

- 每条 trust finding（findings + crosscheck）→ 一条 fail 结果，
  failureCode 由 axis + failure 形状构成（如 TRUST_REPLAY_SILENT_PASS），
  node 定位、evidence、consequence 原样带过来；
- 总体结论 golden.trust.overall 携带 score/penalty/confidence，
  calibration 固定 ordering-only —— audit 分数只表达排序，不是概率；
- 缺 recording.json 时整类「编译时丢掉的事实」检查做不了：
  这是**观测缺失**，报 inconclusive 而不是 fail（缺失不是失败，猜才是）；
- 实测标签（stable-green 等）只进 overall 的 comment，不参与 verdict。
"""

from __future__ import annotations

from pathlib import Path

from trace_eval.contracts import EvaluatorResult

EVALUATOR_NAME = "golden-trust"
EVALUATOR_VERSION = "1.0.0"
_EVALUATOR = {"name": EVALUATOR_NAME, "version": EVALUATOR_VERSION}


class GoldenTrustEvaluator:
    key = "golden.trust"
    version = EVALUATOR_VERSION
    scope = "case"

    def evaluate(self, case_dir: Path) -> list[EvaluatorResult]:
        from trust.audit import audit
        report = audit(case_dir)
        results = [self._finding_result(finding)
                  for finding in report.get("findings", [])]
        results += [self._finding_result(finding)
                    for finding in report.get("crosscheck", [])]
        results.append(self._overall_result(report))
        if not report.get("hasRecording", True):
            results.append(self._no_recording_result())
        return results

    def _finding_result(self, finding: dict) -> EvaluatorResult:
        node = finding.get("node")
        return EvaluatorResult(
            key=f"golden.trust.{finding['failure']}.{node or 'trace'}",
            scope=self.scope,
            verdict="fail",
            calibration="ordering-only",
            failure_code=(f"TRUST_{finding['axis'].upper()}"
                           f"_{finding['failure'].upper()}"),
            node_id=node,
            evidence=[finding.get("evidence") or finding.get("consequence") or ""],
            comment=finding.get("consequence"),
            evaluator=dict(_EVALUATOR),
        )

    def _overall_result(self, report: dict) -> EvaluatorResult:
        findings = list(report.get("findings", [])) + list(report.get("crosscheck", []))
        verdict = "fail" if findings else "pass"
        result = EvaluatorResult(
            key="golden.trust.overall",
            scope=self.scope,
            verdict=verdict,
            score=report.get("score"),
            calibration="ordering-only",
            observed={"steps": report.get("steps"),
                      "penalty": report.get("penalty"),
                      "confidence": report.get("confidence")},
            comment=(f"实测标签：{report['label']}" if report.get("label") else None),
            evaluator=dict(_EVALUATOR),
        )
        if verdict == "fail":
            result.failure_code = "GOLDEN_FINDINGS_PRESENT"
            result.evidence = [f"{f['failure']}@{f.get('node') or 'trace'}"
                               for f in findings]
        return result

    def _no_recording_result(self) -> EvaluatorResult:
        return EvaluatorResult(
            key="golden.trust.recording",
            scope=self.scope,
            verdict="inconclusive",
            calibration="deterministic",
            comment="缺 recording.json：编译时丢掉的事实这一整类检查做不了",
            evaluator=dict(_EVALUATOR),
        )
