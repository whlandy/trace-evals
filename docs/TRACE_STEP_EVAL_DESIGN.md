# Trace 单步与轨迹评估增强设计

## 1. 目标与边界

在现有 `features → rules → score → validate` 证据链上增加两级能力：

1. **单步评估**：为每个 trace 节点输出结构化、多维、可追溯的评分；
2. **轨迹评估**：结合前后文、参考轨迹和任务目标，判断路径是否必要、正确、完整。

模型评审是补充证据，不替代现有确定性规则。模型分数在通过人工标签校准前，必须标记为
`uncalibrated-judge-score`，不能解释为成功概率。

## 2. 借鉴的评估范式

LangChain AgentEvals 将 agent 轨迹评估分为两类：

- 确定性 trajectory match：`strict`、`unordered`、`subset`、`superset`；
- LLM-as-judge：用 rubric 评价完整执行路径，可选参考轨迹。

本项目采用同样的分层，但评估对象不同：AgentEvals 判断“执行是否符合参考”，本项目还要先判断
“参考 trace 本身是否值得信”。因此形成三层：

- **L0 事实/规则**：现有特征、规则和失败形态；
- **L1 单步 judge**：对每一步的选择、参数、验证和上下文一致性评分；
- **L2 轨迹 judge/match**：评价完整路径、遗漏、冗余和最终证据闭环。

OpenAI Graders 的 `score_model`、`label_model` 与 `multi` 形态用于指导本地接口设计：模型评分、
离散标签和确定性子评分必须可组合，但本仓库不强制依赖远程 Evals 服务。

## 3. 统一数据模型

新增 `StepEvaluation`：

```json
{
  "nodeId": "step_0004",
  "scores": {
    "action_correctness": 0.0,
    "argument_quality": 0.0,
    "context_fit": 0.0,
    "evidence_quality": 0.0,
    "replay_safety": 0.0
  },
  "overall": 0.0,
  "label": "pass|weak|risky|fail|unknown",
  "confidence": "deterministic|uncalibrated-judge-score|calibrated",
  "findings": [],
  "reason": "",
  "source": "rules|model|hybrid",
  "grader": {"name": "...", "version": "..."}
}
```

所有维度范围为 `[0, 1]`。`overall` 是排序摘要，不覆盖原始维度和 findings。缺失上下文时使用
`unknown`，不得用中间分数掩盖“不知道”。

新增 `TraceEvaluation`，包含：

- `steps[]`：逐步评分；
- `trajectory`：路径完整性、必要性、顺序合理性、最终证据闭环；
- `match`：可选的 strict/unordered/subset/superset 结果；
- `aggregate`：规则分、judge 分分别保留，再给 hybrid 排序值；
- `provenance`：rubric 版本、模型、采样参数、输入摘要和时间。

`trajectory` 的四个连续分数分别携带 `trajectoryEvidence`。完整性必须引用参考步骤、flow oracle
或 trace finding；必要性引用实际/参考步骤或 trace finding；顺序引用参考步骤或 flow oracle；
证据闭环引用 flow oracle、trace finding 或 oracle check。每项都要求非空 evidence/reason，且
本地 validator 会拒绝没有输入回链的自由文本依据。

## 4. 单步模型如何评分

模型输入不是孤立节点，而是最小充分上下文：

- 任务目标；
- 当前步骤前后各最多 2 步；
- 当前 selector/action/param/expected response/assertion；
- 可用工具或动作规格；
- L0 规则发现；
- 可选参考步骤。

Rubric 固定评五维：

1. `action_correctness`：动作是否推进任务且语义正确；
2. `argument_quality`：选择器、参数、请求预期是否具体且稳健；
3. `context_fit`：是否与前置状态和后继步骤一致；
4. `evidence_quality`：该步是否验证了应验证的业务结果；
5. `replay_safety`：重复执行、环境变化、条件 UI 下是否安全。

模型必须返回严格 JSON，并为低分维度给出引用到 `nodeId`/字段的简短理由。规则已确认的事实作为
约束输入；模型不能将 `blind_toggle` 等硬缺陷改判为无缺陷，只能补充上下文判断。

## 5. 聚合策略

第一阶段不训练权重：

- 硬规则产生 `deterministic_score`；
- 模型五维的算术平均产生 `judge_ordering_score`；
- `hybrid_score` 使用保守门控：硬规则存在 `silent-pass` 时，上限为 0.49；存在
  `loud-later` 时上限为 0.74；否则才采用 judge 排序值；
- trace 聚合同时报告均值、最低步骤和 bottom-k 均值，避免大量正常步骤稀释一个致命步骤。

这些值只用于排序。拿到足够人工/重复回放标签后，再用单调逻辑回归或 isotonic regression 校准，
并报告 AUC、ECE、Brier score、bootstrap 置信区间。

## 6. Judge 的 meta-eval

模型 judge 上线前必须通过：

- **结构有效率**：严格 JSON 解析率 100%；
- **变异敏感性**：现有 mutation 注入后，对应步骤分数严格下降；
- **重复一致性**：固定模型、rubric、seed 后，标签一致率和分数方差可报告；
- **规则冲突率**：模型不得翻转确定性硬事实；
- **失败节点命中**：在现有标签上，不低于规则层的 5/6；
- **人工一致性**：新增盲评集后计算 Spearman/加权 kappa。

样本仍为 1 绿 6 红时，不宣称校准完成，也不把“模型更会解释”写成“模型更准确”。

## 7. 模块与接口

计划新增：

```text
trust/eval_types.py       评分 schema、校验与序列化
trust/step_score.py       L0 逐步确定性评分与聚合
trust/trajectory_match.py 四种轨迹匹配模式
trust/judge.py            Judge 协议、prompt/rubric、结构化输出解析
trust/providers/          可插拔模型 provider；默认无网络依赖
trust/hybrid.py           规则 + judge 保守聚合
trust/judge_validate.py   mutation、一致性、冲突率 meta-eval
```

CLI 入口：

```bash
python3 -m trust.step_score TRACE
python3 -m trust.judge TRACE --goal "..." --provider openai
python3 -m trust.judge_validate TRACE...
```

没有 API key 或 provider 时，L0 单步评分和 trajectory match 必须照常可用。

## 8. 分期与验收

### M1：逐步确定性评分骨架

- 建立 schema 和 L0 映射；
- 每个节点都有评分，finding 精确归属到节点；
- trace 级旧分数保持兼容；
- 单测覆盖无 finding、silent-pass、trace 级 finding 三类。

### M2：轨迹匹配

- 实现 strict/unordered/subset/superset；
- 区分 tool/action 名、参数、顺序与遗漏；
- 对差一个步骤的轨迹给部分差异，不只给布尔值。

### M3：模型 judge

- provider 协议、版本化 rubric、严格结构化输出；
- 单步与全轨迹两种模式；
- 缓存输入哈希，确保实验可复现和控制成本。

### M4：meta-eval 与校准准备

- mutation 逐步降分率达到 100%；
- 规则冲突率为 0；
- 现有失败节点命中率不低于 5/6；
- 输出可直接用于后续人工标注和校准的数据集。

## 9. 当前先做什么

从 M1 开始。它不需要新标签或外部模型，却能建立后续 judge 必须遵守的输出契约，并立即满足
“给每一个步骤打分”的基础能力。M1 完成后再接 M2；M3 必须在可复现与 meta-eval 框架存在后接入。

## 10. 参考资料

- LangChain, *How to evaluate your agent with trajectory evaluations*：
  https://docs.langchain.com/langsmith/trajectory-evals
- LangChain, *Application-specific evaluation approaches*：
  https://docs.langchain.com/langsmith/evaluation-approaches
- LangChain, *Evaluate a complex agent*：
  https://docs.langchain.com/langsmith/evaluate-complex-agent
- OpenAI, *Graders API reference*：
  https://developers.openai.com/api/reference/resources/graders
- OpenAI, *Evals API reference*：
  https://developers.openai.com/api/reference/resources/evals
