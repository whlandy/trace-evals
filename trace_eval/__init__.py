"""trace-evals/v2 迁移包（Strangler 模式的新边界）。

设计文档：docs/EVAL_PLATFORM_MIGRATION_DESIGN.md。

原则：
- 不重写 trust/ 现有实现 —— 新代码只做协议适配与实验架构；
- 每个结果都携带证据和来源（contracts.EvaluatorResult）；
- 未经校准的数值只能排序，不得命名为概率。
"""

__version__ = "0.1.0"
