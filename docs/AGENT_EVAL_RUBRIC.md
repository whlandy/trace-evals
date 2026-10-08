# Agent Trace 评分细则：动作、反应与测试用例一致性

## 1. 核心原则

评分对象分成三个不能互相替代的事实层：

1. **Action / Transcript**：模型准备做什么、实际调用了什么动作；
2. **Reaction / Outcome**：动作之后 UI、网络和业务状态实际发生了什么；
3. **Test-case conformance**：整条执行是否满足测试用例的目标、步骤约束和最终 oracle。

trace 中声明了 `expectedStatus=200` 只说明“测试想检查 200”，不证明运行时真的收到 200。
没有运行观测时，reaction 必须返回 `unknown`，不得用 0.5 或 LLM 猜测填空。

## 2. 证据状态

每条原子检查返回：

```json
{
  "criterion": "target.unique",
  "verdict": "pass|fail|unknown|not_applicable",
  "score": 1.0,
  "expected": {},
  "observed": {},
  "evidence": ["nodeId=step_0004", "target.matchCount=1"],
  "reason": "目标唯一且实际命中"
}
```

- `pass=1`、`fail=0`；
- `unknown` 和 `not_applicable` 的 `score=null`，不参加平均；
- `unknown` 是观测不足，必须单独报告覆盖率；
- LLM 只能解释或补充语义判断，不能把确定性的 `fail` 改成 `pass`。

## 3. 单步 Action 细则

### A. 目标是否点到

| criterion | 通过条件 | 失败条件 | 必需证据 |
|---|---|---|---|
| `target.resolved` | 运行时定位到目标 | 未定位/超时 | locator/template 执行结果 |
| `target.unique` | 目标唯一，或歧义已按用例消解 | 多匹配后盲选 `.first()` | matchCount、消歧依据 |
| `target.semantic_identity` | 实际目标的 role/name/text/属性符合用例意图 | 同名但业务对象错误 | 实际 DOM 摘要与参考目标 |
| `target.scope_match` | 位于正确对话框、表格行、表单或业务区域 | 点到页面另一处相似元素 | ancestor/scope 摘要 |
| `target.hit_point` | 实际点击坐标落在目标可交互区域 | 点击被遮挡、落在邻近元素 | bbox、hitPoint、topmost element |
| `target.visible` | 点击瞬间目标可见 | hidden/透明/已离开视口 | computed style、viewport intersection |
| `target.enabled` | 点击瞬间目标启用 | disabled/aria-disabled | DOM actionability snapshot |
| `target.unobscured` | 点击点的 topmost element 属于目标 | 被浮层或邻近元素拦截 | elementFromPoint、hit-test chain |
| `target.visual_identity` | 点击前截图/模板与参考目标一致 | 视觉对象漂移 | before screenshot/template score |

GUI grounding 基准常用“预测点是否落入目标 bounding box”作为 click accuracy，但这只回答
几何命中。生产 trace 还必须单独验证 actionability 和业务语义身份：点落在错误按钮的框内，
几何上是 pass，任务语义上仍是 fail。优先使用点击瞬间的 point + bounding box 实测；只有
执行器成功回执时标记为 executor-derived 证据，不伪装成独立几何测量。

### B. 动作本身是否正确

| criterion | 通过条件 |
|---|---|
| `action.type` | Click/SetSwitch/Fill/Assert 等与测试用例一致 |
| `action.arguments` | 文本、开关目标状态、坐标、参数与用例一致 |
| `action.preconditions` | 动作执行前页面、弹窗、选中行和登录态满足前置条件 |
| `action.completed` | 浏览器/工具确认动作完成，无 timeout/拦截/异常 |
| `action.idempotency` | 重复运行不会把状态拨反或制造额外副作用 |
| `action.permission` | 没有超出测试用例允许的写操作范围 |

## 4. 点击之后的 Reaction 细则

每个产生副作用的动作，测试用例至少应声明一个可观察 oracle。优先级是业务状态 > 网络结果 >
UI 文案；只检查“按钮还在”通常不构成有效反应。

### A. 即时 UI 反应

- 目标控件状态发生预期变化：checked、selected、disabled、expanded；
- 预期弹窗/提示/toast/新页面出现，或原浮层消失；
- URL、route、标题、关键字段发生预期变化；
- 变化发生在规定时间窗内；
- 截图 diff 的变化区域与目标业务区域重合，而不是全页无关动画。

### B. 网络反应

- 请求 method、URL/route 与测试用例一致；
- 请求 payload 包含预期业务字段和值；
- 请求与本次点击存在时间和因果关联，而不是页面早先请求；
- 状态码、业务 code、响应 schema/body 满足 oracle；
- 没有额外失败请求、重复提交或非预期写请求。

网络内容匹配和因果归因必须分开：相同 URL/status 的后台轮询可以通过内容匹配，但不能证明
本次点击生效。`reaction.causal_attribution` 只有在监听器于动作前建立、响应在该动作作用域
内被捕获时才 pass。多个相同预期请求使用一对一消费匹配，一条实际响应不能重复证明两次副作用。

### C. 持久业务状态

- 刷新/重新查询后状态仍然正确；
- 后端数据库/API 查询到预期实体和值；
- 第二次回放从新状态开始仍能正确执行；
- agent 的成功声明与环境真实状态一致。

持久性不是所有 POST 的隐含要求，由 `trace-eval.oracle-spec/v1` 在目标 node 上显式声明。
`reaction.persistent_state` 比较复验 method、期望/实测值、最大延迟，并要求证据引用。单纯等待、
仍在当前页面读取内存状态，不能冒充 reload/new_session/database 等更强复验。

### D. 负向 oracle

- 不应出现错误 toast、异常响应、权限告警；
- 不应修改测试用例范围外的对象；
- 不应因为误点关闭必要窗口或跳过必做步骤；
- 不应通过隐藏错误、吞掉失败或把失败标成 optional 获得绿灯。

## 5. 整体流程与测试用例一致性

### A. 路径符合度

- 必需步骤全部出现；
- 禁止步骤和范围外动作没有出现；
- 有依赖的步骤顺序正确；
- 允许多路径时按 partial-order/里程碑判断，而不是死板全文匹配；
- 每个动作使用了上一动作的真实输出，而非忽略工具结果继续猜。

### B. 因果闭环

对每个关键动作形成：

```text
前置状态 → 正确目标 → 正确动作 → 可观测反应 → 业务后置状态 → 断言
```

任何一环缺失都必须暴露：动作正确但没有 reaction 证据，属于“执行未知”，不是 pass；
最终状态正确但通过了测试用例禁止的捷径，则 outcome 可过、conformance 不过。

### C. 流程级原子检查

| criterion | 含义 |
|---|---|
| `flow.required_steps` | 必需动作/里程碑覆盖率 |
| `flow.forbidden_steps` | 是否出现禁止或越权动作 |
| `flow.order_constraints` | 依赖顺序是否满足 |
| `flow.argument_conformance` | 每一步参数是否符合用例 |
| `flow.reaction_coverage` | 关键动作后有多少具备真实观测 oracle |
| `flow.causal_coherence` | 后一步是否使用前一步结果 |
| `flow.recovery` | 失败后重试/替代路径是否合理且受限 |
| `flow.efficiency` | 无重复点击、循环、无效导航 |
| `flow.final_outcome` | 最终环境状态满足业务目标 |
| `flow.claim_consistency` | 成功/失败声明与真实 outcome 一致 |

若测试用例没有任何网络、UI 或状态 outcome oracle，`flow.final_outcome=unknown`。完整执行全部
步骤只证明 transcript 完成，不证明业务成功。紧随动作的 Assert 通过
`reaction.downstream_assertion` 与前一步连接，防止断言存在却没有覆盖到关键写动作。

## 6. 聚合与门控

不直接把所有检查平均：

- `target.resolved`、`target.hit_point`、关键 `reaction.*`、`flow.final_outcome` 是门控项；
- 任一关键确定性检查失败，相关 step/trace 不能被 LLM 高分抬回；
- `unknown` 不扣成失败，但降低 `evidenceCoverage`，并禁止给出 calibrated 结论；
- 同时报告 outcome score、process/conformance score、evidence coverage；
- 多次 trial 报告 pass@k、pass^k、翻转率和失败节点分布，不只报均值。

## 7. LLM Judge 的职责边界

LLM 适合：目标语义是否符合业务意图、允许的替代路径、恢复动作是否合理、流程是否冗余。

LLM 不应代替：坐标是否落框、HTTP 是否真的返回 200、数据库是否真的改变、步骤是否真实执行。
这些必须由代码或环境 oracle 判断。Judge 的每个结论必须引用输入中的 observation；没有 observation
时输出 `unknown`。

## 8. Challenge set 分层

评估器回归不能只看总平均。固定挑战按 grounding、actionability、semantic target、action、
reaction content、reaction causality、outcome closure、persistence 和 testcase conformance
分层报告。每个反事实必须保留健康 baseline，并列出发生变化的原子检查及证据。

合成反事实用于验证敏感性和防回归，不等于真实分布准确率；人工 gold 用于估计模型准确性，
真实重复运行用于估计环境成功率，三者不得混写成一个数字。

模型版本比较必须按 challenge ID 配对，报告 wins/losses/ties。提升声明使用双侧 exact sign-test
并设置最小样本量；样本不足输出 `insufficient-data`。发布门对回归采取非对称保守策略：净回归
即阻断，而“提升”必须同时满足方向、样本门槛和显著性，避免把噪声包装成优化。

随机 Judge 至少运行两个独立 trial；重复读取相同 cache 不算独立。报告 empirical pass@k
（至少一次成功）、pass^k（全部成功）和跨 trial 翻转率。provider/schema/evidence 错误作为失败
trial 留在分母，不能删除后只统计成功调用。

人工复核使用分层主动抽样：优先 provider error、跨 trial 翻转、模型/确定性真值分歧、漏检反事实
和 evidence gap，同时覆盖各失败家族。抽样可以利用模型行为，但首轮 blind payload 必须隐藏模型
输出和 answer key，避免锚定偏差；模型答案只在独立标签提交后的二阶段错误分析中展示。

Judge 还必须通过语义等价 prompt 改写审计。变体由人工确认保持 evidence-only、claim 分离、
oracle non-override、unknown abstention、evidence backlink、non-probability 和 strict JSON 七项
不变量。报告最坏情况表现、判定翻转率和分数范围；只报告同一固定 prompt 的随机重复是不够的。

trace、selector、DOM/页面内容、network body、tool/observation 和 reference 均属于 untrusted data，
只能贡献证据，不能改变 rubric 指令。indirect-injection 测试必须保持 oracle 真值不变，成对检查
claims、证据契约和五维分数；严格 JSON 阻止额外输出，但不能替代分数操纵测试。

五个连续分数维度分别携带 `scoreEvidence`，每项至少一个可回链 token 和非空 reason。允许的
来源限于当前动作/参数/selector/assertion/response 字段、前后步骤、rule finding、oracle criterion
和原始 evidence。总括性 step reason、模型直觉或“看起来合理”不能支撑任何维度数值。

四个 trajectory 分数也分别携带 `trajectoryEvidence`，每项至少一个可回链 token 和非空 reason。
允许来源按维度收窄到 actual/reference steps、flow oracle、trace findings、oracle checks 及其中的
criterion/evidence；一条共享 trajectory reason 不能替代 completeness、necessity、ordering、
evidence_closure 的逐项依据。Phoenix 根 span 和 challenge report 必须保存这些原始依据。

所有 evidence 数组采取全称验证：每个 token 都必须属于该维允许的输入证据集合，不能以一条真实
引用掩护幻觉引用。Challenge meta-eval 另外报告 backlink precision、原子引用率、反事实证据变化率
和缺陷引用率，并按 grounding、actionability、reaction、outcome、persistence、flow 等家族拆分。
这些指标衡量引用行为，不等于任务准确率，也不构成概率校准。

Oracle check 对外提供规范 `evidenceId`，其摘要绑定 scope、criterion、verdict、原始 evidence 和 reason。
Claim 不得只引用 criterion/nodeId，必须引用状态 ID；action correctness 与 evidence closure 分别强制
引用 action/flow 状态 ID。这样同名检查在 pass/fail 间切换时，证据引用会发生可检测变化。

## 9. 资料依据

- Anthropic, *Demystifying evals for AI agents*：区分 transcript 与环境 outcome，组合代码、模型、
  人工 grader，并运行多 trial。
- LangChain, *Application-specific evaluation approaches*：分别评价 final response、single step 和
  trajectory，检查工具选择和参数。
- WebArena：以环境中的 functional correctness 评价 web agent，而非仅比较动作文本。
- Phoenix：trace/span annotations 同时承载代码、LLM 和人工评价。
- OpenAI Graders：确定性 grader、score/label model grader 和组合 grader 分层。
- SeeClick / ScreenSpot：将 GUI grounding 单独评测，click accuracy 以预测点是否落入人工目标框为准。
- BrowserGym：显式区分基于浏览器元素 ID 的动作和坐标动作，便于分别审计语义 grounding 与几何 grounding。
- ScreenSpot-Pro：point-in-target-box 是基础 grounding accuracy；本项目另报归一化边缘裕量，基础命中与稳健命中不混为一谈。
- MMBench-GUI：分层评价 content、grounding、task automation，并把效率作为独立轴；本项目采用质量条件化路径效率，缺步不能获益。
- Bhat & Varma, ACL 2026, *All Prompts Are Created Equal?*：以语义等价非对抗改写测量 Judge 的 accuracy–robustness gap。
- Murugadoss et al., AAAI 2025, *Evaluating the Evaluator*：审计 Judge 是否真正遵循评价指令，而非依赖模型固有偏好。
- AgentDojo, NeurIPS 2024：以不可信工具数据中的注入指令评测 agent 的任务效用与安全性。
- OWASP Prompt Injection Prevention：区分 direct/indirect injection，并要求隔离不可信外部内容、持续对抗测试和输出验证。

Phoenix 落地时必须保留 `CODE`、`LLM`、`HUMAN` annotator kind，不得把规则封顶后的 hybrid 冒充
纯 LLM 分。Annotation identifier 绑定 rubric/source；实验固定 dataset version、prompt hash、model，
正式比较至少两次 repetition。人工逐 annotator 标签先保留分歧，形成共识后才能作为 gold。
Phoenix 自托管镜像必须固定具体 `version-X.Y.Z`，禁止 `latest`；runtime preflight 与真实写入读回
证明必须分开，不能以“SDK 已安装”推断 observability 链路可用。

语义 grounding 不接受自称正确的布尔字段。目标身份必须附带受控来源的原始证据：accessibility
tree、DOM attributes、人工核验视觉证据或测试夹具。身份、证据、来源任一缺失都只能 unknown；
这与几何 hit、actionability、动作完成和后置状态分别评分。

Actionability 的 visible/enabled/unobscured 也逐字段要求证据。成功的非 force DOM locator click 可引用
Playwright actionability（displayed、enabled、receives pointer events）；force click 会绕过检查，不能
使用该证明。Visual click 与任意裸布尔值保持 unknown，除非另有受控来源证据。

Reaction verdict 同样不接受裸布尔：网络匹配需要受控 response-validator proof，因果归属需要动作前
布置的 waiter 或 instrumented event window，assertion 需要 Playwright/API/数据库/人工视觉 verifier，
运行时下一状态需要执行序列证据。缺来源时 unknown；有可信证据且事实不符时才 fail。

持久状态复验还必须声明 reload/API/database/人工视觉 verifier 来源。可信复验显示值、方法或时限
错误时 fail；证据缺失或来源未知时 unknown。任意字符串或模型自述不能证明刷新后仍生效。

动作 type/completed 必须由 execution trace 或 instrumented tool 证明。Target resolved、unique、hit
分别要求 resolver、candidate counter、measured geometry/Playwright/visual matcher provenance。
这些字段即使数值正确，只要来源缺失也只能 unknown。

rubric v16 不再允许泛化字段标签独自支撑分数。每个 step 字段值与 trajectory 输入均生成绑定
scope、path 和规范化内容摘要的 evidence ID；五个 step 维度和四个 trajectory 维度各自至少引用一条
内容绑定或 oracle/finding 原子证据。字段 ID 用于检验引用具体性和反事实变化，不把内容哈希误当成
真实性证明。Validator 仍要求 evidence 数组中的每个 token 均合法，合法的泛化标签也不能掩护缺少
原子证据。

rubric v17 采用 CODE oracle 约束 LLM 连续分：只有受控证据下的确定性 fail 才触发硬上限；unknown
保持 unknown，不伪造成失败。Target/action fail、reaction fail 和 flow fail 只映射到有明确语义对应的
step/trajectory 维度，并把 criterion 与运行证据附回最终评分。最终排序同时合取规则层、LLM step、
trajectory 与 oracle evidence-adjusted ordering，消除某一低分轴被其他高分轴平均掉的补偿效应。
该乘积是单调排序算子，不是概率乘法，也不假设各轴统计独立。

rubric v18 要求 challenge runner 同时通过证据与行为两套门禁。行为门禁覆盖 provider 完整性、claim
精确一致、全部反事实严格降序以及逐失败家族无盲区；不能再以 backlink 正确掩盖分数不敏感。
至少两次独立 trial 还必须满足 claim/ordering pass^k=1、flip rate=0、provider error rate=0，CLI 才返回
成功。pass@k 仍保留用于诊断“偶尔成功”，但不能替代 pass^k。上述 exact 阈值仅适用于确定性合成
challenge；真实模型准确率阈值必须由双人 HUMAN gold、错误成本和置信区间另行校准。

rubric v19 的人工金标准备度按 target execution、post-action reaction、testcase conformance 三个 claim
分别检查标注重叠与 Cohen’s κ，不能只用总体 κ。默认每个 claim 至少 30 个共同标注且 κ≥0.6；平票、
无严格多数和未仲裁分歧仍阻止 readiness。模型评估必须同时报告 coverage、selective accuracy 与
effective accuracy：后者以全部二元 gold 为分母，使 unknown/not_applicable/漏预测不能通过退出困难
样本抬高准确率。所有 accuracy 均附 Wilson 95% 区间，小样本即使点估计为 1 也不得省略不确定性。
