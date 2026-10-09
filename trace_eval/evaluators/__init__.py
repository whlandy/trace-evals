"""Round 1 起新增的 adapter evaluators。

每个 evaluator 只做协议适配：包装 trust/ 的既有实现，把私有返回结构
转成统一的 EvaluatorResult[]。评分逻辑不移动、不改一行（Strangler 模式，
设计 §15）——后续 Runner 不再直接解析各 evaluator 的私有返回结构。
"""

from trace_eval.evaluators.execution import MaaExecutionEvaluator
from trace_eval.evaluators.golden_trust import GoldenTrustEvaluator

__all__ = ["GoldenTrustEvaluator", "MaaExecutionEvaluator"]
