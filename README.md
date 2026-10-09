# trace-evals

统一处理两个彼此独立、但经常被混为一谈的问题：

1. [edr-cloud-recorder](../edr-cloud-recorder) 产出的 golden 轨迹**值不值得信**；
2. maa-fw 的某一次执行是否完整、准确地遵循了这条 golden 轨迹。

它们必须分别报告：一次回放满分，不代表作为参照物的 golden 轨迹有足够证据力。

## trace-evals/v2（Experiment 架构）

新协议层已迁入 `trace_eval/` 包（Strangler 模式：旧 `trust/` CLI 在兼容期并行，
不重写）。五层正确性判定：C1 结构 ∧ C2 执行 ∧ C3 业务 Oracle ∧ C4 Golden 可信
∧ C5 稳定性；缺证据的层返回 `inconclusive`，不得自动按通过处理。

```bash
# 1) 版本化 Dataset 验证 / 快照
python3 -m trace_eval.datasets validate <snapshot-dir>
python3 -m trace_eval.datasets snapshot <dataset-dir> --out <store>
# 2) 执行 Experiment（回放 + Oracle + 重复 Trial + 状态策略 + 稳定性）
python3 -m trace_eval.runner run <snapshot-dir> --trials 2
# 3) 聚合与 Baseline/Candidate 比较（新增失败 / 修复 / 漂移 / 无效 分列）
python3 -m trace_eval.aggregate aggregate <experiment-dir>
python3 -m trace_eval.aggregate compare <baseline-dir> <candidate-dir>
# 4) CI Gate（稳定退出码：0 放行 / 1 阻断 / 2 配置或基础设施错误）
python3 -m trace_eval.gates check <experiment-dir> [--baseline <dir>] [--policy <policy.json>]
# CI：固定 Dataset + Gate 的重复、可解释发布判断
bash ci/smoke.sh
```

**旧 CLI 兼容期与弃用条件**：`trust/` 的旧 CLI（`audit.py`、`maa_execution` 等）
在兼容期内保持可用，核心结果与 Round 0 基线逐字节一致（由
`test/fixtures/contracts/golden-results/` 冻结 + `test/test_e2e_smoke.py`
的基线对拍锁定）。弃用条件：v2 Gate 连续两个发布周期稳定阻断、且
Baseline 比较（`trace_eval.aggregate compare`）无回归后，旧 CLI 方可标记弃用。

## Golden 轨迹可信度体检

```bash
python3 trust/audit.py <轨迹目录>
```

## Maa 执行结果评分

```bash
python3 -m trust.maa_execution \
  --golden /path/to/maa-trace.json \
  --execution /path/to/maa-execution.json \
  --output /path/to/maa-evaluation.json
```

这个评分器校验 golden digest、节点集合与顺序，并输出完成率、动作准确率、轨迹
顺序、重试效率和视觉匹配置信度。它只回答“这一次是否按编码好的路径执行”，不把
高分解释为业务结果正确。

## 为什么两种结果必须分开看

Maa execution evaluator 评的是「**这一次回放**跑得怎么样」。
它答不了「该不该信这条轨迹」—— 实测语料里最好的那条：

    ══ maa-flow6 ══  9 步 · 分数 50（罚分 50）
       实测标签：stable-green      ← 连跑两遍都成功，回放 100 分
       [悄悄绿] 整条轨迹：9 步，0 条断言
           → 这条轨迹只证明「这串操作能走完」，不证明系统做对了任何事

**回放 100 分,可信度 50。** 满分不等于可信。

## 拆成三根正交的轴

| 轴 | 问题 |
|---|---|
| 可回放性 | 再跑一次还会绿吗 |
| 证据力 | 它绿了能说明什么 |
| 观测充分性 | 我们凭什么给出上面两个判断 |

发现项按**失败形态**分类，而不是拍一个「严重度」——
形态是事实，严重度是伪装成事实的权重：

    silent-pass   悄悄绿：做错了照样报成功。会变成虚假的信心，比失败更伤
    flaky         时好时坏：换个起点结论就变
    loud-later    当时不报，以后才炸：录完当场全绿，隔几小时就红
    weak          不会失败，但它绿了也说明不了什么

## 分数只表达排序

每份报告都带 `uncalibrated-ordering-only`。罚分的四个数字是拍脑袋的，
**只有相对顺序有依据**。校准需要一批「稳定绿 / 翻转 / 稳定红」都够的标签，
而实测出来是 1 绿 6 红 —— 这个比例算不出校准误差。
说成概率就是撒谎。

## 模块

```text
trust/features.py    从轨迹抽事实，不打分
trust/rules.py       事实 → 带证据的发现项（每条都有 evidence 和 consequence）
trust/crosscheck.py  recording.json ↔ trace.json，捞编译时丢掉的事实
trust/score.py       罚分汇总（只表达排序）
trust/mutate.py      10 种缺陷注入器，给评估器造负样本
trust/replay_lab.py  重复回放取真值标签
trust/validate.py    meta-eval：评估这个评估器
trust/audit.py       一条命令的体检报告
trust/maa_execution.py  Maa 单次执行的确定性评分
```

## 逐步骤与模型评审

现有规则层现在也可以给每一步输出 action correctness、argument quality、context fit、
evidence quality、replay safety 五维评分：

```bash
python3 -m trust.step_score <轨迹目录>
python3 -m trust.trajectory_match <实际轨迹> <参考轨迹> strict
```

需要上下文语义判断时，可使用可插拔 judge。OpenAI provider 通过 Responses API 的严格
JSON Schema 输出；没有 API key 时，上面的本地规则评分和轨迹匹配仍可正常使用：

```bash
python3 -m trust.evaluate <轨迹目录> --goal "策略保存且副作用得到验证" --model <模型名>
python3 -m trust.evaluate <实际轨迹> --reference <参考轨迹> --goal "..." --model <模型名>
python3 -m trust.judge_validate <轨迹目录> --goal "..." --model <模型名>
```

批量运行使用 `trace-eval.case/v1` JSONL manifest；每条 case 的 `trace/reference/observations/oracleSpec`
既可内嵌对象，也可填写相对 manifest 的 JSON 路径：

```bash
python3 -m trust.pipeline cases.jsonl --model <模型名> --out evaluation-report.json
# 严格流水线：任一 case 需要人工复核时也返回非零
python3 -m trust.pipeline cases.jsonl --model <模型名> --out evaluation-report.json \
  --fail-on-review
```

批处理隔离单行 JSON、字段、引用文件及 provider/validator 错误，保留其余 case，并记录错误位置；
存在错误时返回非零。有效 case 的重复 ID 属于整批配置错误；所有未校准结果默认进入复核路由。

Judge 输出、hybrid 输出都标记为 `uncalibrated-judge-score`。它们只表达 rubric 下的排序，
不是成功概率；规则确认的硬缺陷会给模型分数设置上限，模型不能将其覆盖。
v22 在 `judge` 字段保留校验通过、合并前的模型输出；顶层 `steps/trajectory` 是 Hybrid 结果。
Challenge 的模型证据审计使用前者，不计入 Hybrid 自动补充的证据。节点对齐、可选/额外步骤规则见
[精简架构设计](docs/AGENT_TRACE_EVAL_ARCHITECTURE.md)。

## Phoenix 可视化与实验

Phoenix 只作为 trace、annotation、dataset 和 experiment 平台；评分判据仍在本仓库版本控制。
本地启动：

```bash
docker compose -f docker-compose.phoenix.yml up -d
python3 -m pip install -r requirements-phoenix.txt
```

将规则层逐步评分发送到 `http://localhost:6006`：

```bash
python3 -m trust.phoenix_export <轨迹目录>
```

若已经运行了模型评审，可将完整 hybrid 结果作为 annotations 导出：

```bash
python3 -m trust.evaluate <轨迹目录> --goal "..." --model <模型名> > evaluation.json
python3 -m trust.phoenix_export <轨迹目录> --evaluation evaluation.json
```

如果有真实回放观测，把它交给 judge；缺失的 reaction 会明确显示为 `unknown`：

```bash
python3 -m trust.observations <轨迹目录> execution.json --out observations.json
python3 -m trust.evaluate <轨迹目录> --goal "..." --model <模型名> \
  --observations observations.json > evaluation.json
```

`trust/replay_lab.py` 新产生的每次 run 会直接包含 `observations`，无需再手工转换。

先检查、不联网发送：

```bash
python3 -m trust.phoenix_export <轨迹目录> --dry-run
```

Phoenix annotation 严格区分来源：确定性规则和 conservative hybrid 标为 `CODE`，原始 Judge
claim/trajectory 标为 `LLM`，双人盲标通过 `trust.phoenix_human` 标为 `HUMAN`。自动 annotation 的
identifier 包含 rubric version，防止新 rubric 覆盖旧实验；人工 annotation 使用逐 annotator 稳定
identifier，支持幂等修改且保留标注者间分歧。

```bash
python3 -m trust.phoenix_human human-labels.jsonl span-ids.json --dry-run
```

正式 Phoenix 实验通过 `trust.phoenix_experiment.run_versioned_experiment` 运行：必须固定 dataset
version、rubric version、prompt hash 和 model，且 `repetitions>=2`；单次运行仅允许 dry-run。重复实验
指标仍不是校准概率。

`python3 -m trust.calibrate human-labels.jsonl` 同时输出 human-gold readiness gate。默认要求至少两名
标注者、无待仲裁、总 gold≥50、每个 claim gold≥30 且同时覆盖 pass/fail；未满足时非零退出。
门槛可通过 CLI 参数调整并应随报告保存，但不能为了得到绿色结果而事后降低。

Phoenix server 镜像固定为 `arizephoenix/phoenix:version-20.4.0`，禁止 `latest`。启动或 CI 前运行：

```bash
python3 -m trust.phoenix_preflight
```

预检分别报告 compose pin、client/OTel 版本、HTTP 可达性与 Docker fallback。通过只代表环境可启动或
服务可达，不代表 span/annotation 已完成写入读回；端到端证明必须另行保存实际导出结果。

真实服务可用后使用 `python3 -m trust.phoenix_export ... --verify`。该模式导出后按实际
`context.span_id` 读回 spans 和 annotations，逐项核对 name、annotator kind、rubric/source identifier、
label/score/explanation；完全一致才返回成功并生成 `proofHash`，缺失或多余 annotation 均非零退出。

Phoenix 中一条 trace 对应根 span，每个 trace node 对应一个子 span；五维分数和 overall
作为 span annotations 写入。annotations 的 metadata 始终带
`ordering-only-not-a-probability`，避免 UI 中的数值被误读成成功概率。
模型还会为 `target_execution`、`post_action_reaction`、`testcase_conformance` 分别输出
pass/fail/unknown claim；这些 claim 同样显示在对应节点的 Phoenix annotations 中。

## 人工盲标与 Judge 准确性

模型准确性不能用模型自己的分数证明。`trust.calibrate` 接受至少两位标注者独立给出的
claim 级 JSONL，以严格多数形成 gold；人数不足或平票进入人工仲裁：

```json
{"case":"checkout-01","nodeId":"step_0002","claim":"target_execution","verdict":"pass","annotator":"reviewer-a","evidence":["screenshot:before-after-002"]}
```

```bash
python3 -m trust.calibrate human-labels.jsonl --predictions judge-claims.jsonl
```

报告包含标注者两两 Cohen's kappa、三类 claim 各自的 precision/recall/F1、已知预测覆盖率
和弃权数。`unknown` / `not_applicable` 不会偷偷算成正确，也不会混入二分类 F1。

只有 prediction 显式提供 `probability`、至少 30 条有效 gold 且 pass/fail 两类都存在时，
才计算 Brier score 和 ECE。现有 `overall`、`orderingScore`、
`evidenceAdjustedOrderingScore` 都不会被当成概率。

人工标签完成仲裁后，可以生成 Phoenix golden dataset。每条 claim 独立成为一个 example，
输入保存步骤上下文与 oracle，reference output 保存人工 verdict/evidence，metadata 保存 case、
nodeId、rubric 版本和投票来源：

```bash
python3 -m trust.phoenix_dataset human-labels.jsonl \
  --case checkout-01 ./traces/checkout-01 \
  --oracle-spec checkout-01 ./oracle-spec.json \
  --goal "完成结账并验证订单成功" --dry-run
```

去掉 `--dry-run` 后上传到 Phoenix。存在未仲裁标签时命令会拒绝上传。
`trust.phoenix_dataset.compare_judges` 提供逐 claim 的配对回归门：候选 Judge 的 F1 或
coverage 下降就失败；缺失整条预测仍保留在 gold 分母中，不能通过漏答隐藏困难样本。

上传使用稳定的 `example_id_key` 做 Phoenix dataset version diff：同一 case/node/claim 重跑会
更新原 example，不会静默追加重复样本。oracle spec 的节点内容进入 input，完整 spec 的 SHA-256
进入 metadata，保证不同业务判据的实验可区分、可追溯。

点击命中现在不是一个布尔值：oracle 分开检查 resolved、唯一匹配、point-in-bounding-box、
visible、enabled、unobscured 和 semantic identity。运行记录如果提供 `point` 与
`boundingBox`，会进行真实几何计算；缺少 actionability 或语义身份字段时返回 `unknown`。
所以“Playwright 没报错”不等于“点中了测试用例要求的业务对象”。

Reaction 也拆成“内容正确”和“因果正确”：匹配 URL/status/body 的后台请求只能通过
`reaction.network`，只有动作前建立的 response waiter 在该动作作用域捕获到响应，才通过
`reaction.causal_attribution`。相同请求按一对一消费匹配，不能用一条响应满足多个预期。
如果下一步是 Assert，还会生成 `reaction.downstream_assertion`，把业务状态验证连回触发它的
动作。没有任何 outcome oracle 的轨迹一律 `flow.final_outcome=unknown`。

需要验证刷新或重新查询后仍然生效时，用独立 oracle spec 声明持久性要求，避免修改 recorder
的严格 trace schema：[oracle-spec.example.json](/Users/edr-test/ai-projects/trace-eval/oracle-spec.example.json)

```bash
python3 -m trust.evaluate <轨迹目录> --goal "保存并持久生效" --model <模型名> \
  --observations observations.json --oracle-spec oracle-spec.json
```

`reaction.persistent_state` 支持 `reload`、`requery`、`api`、`database`、`new_session`。
运行 observation 必须提供相同 method、精确 observed 值、是否通过、耗时和至少一条证据引用；
缺观测为 unknown，错误方式、错误状态、超过最大延迟或空证据均为 fail。

## 固定 challenge set

用一条覆盖完整特征的健康参考轨迹生成配对挑战集：

```bash
python3 -m trust.challenge_set <健康参考轨迹> --case policy-save \
  --out trace-eval-challenges.jsonl
```

当前固定集合包含 2 个 control 和 30 个 counterfactual，覆盖 grounding、grounding-margin、actionability、
目标语义、动作执行、反应缺失/错误/因果、outcome assertion、持久状态和测试用例一致性。
每条记录自包含 trace、reference、observations、oracle spec、逐步 claim、流程 verdict、规则结果
及相对基线发生变化的原子证据。

challenge 自检要求每个反事实至少被职责正确的确定性层捕获且排序降低。例如
`drop_template` 不改变动作 transcript，轨迹 oracle 本来就不该假装看见它；该缺陷由静态规则层
捕获。challenge 真值标记为 synthetic counterfactual，不能冒充人工 gold 或校准概率。

对任意 Judge provider 批量运行并按 failure family 查看结果：

```bash
python3 -m trust.challenge_run trace-eval-challenges.jsonl \
  --goal "保存策略并验证持久生效" --model <模型名> > challenge-report.json
```

报告分别给出 claim exact accuracy 和相对健康基线的 ordering sensitivity。单条 provider/schema/
证据违规会隔离成 `provider-error`，其余挑战继续执行。当前 rubric v3 强制每个 claim 至少引用
一个相关 oracle criterion、原始 evidence token 或 `nodeId=...`；空证据和虚构依据都会拒绝。

比较两个模型/rubric 版本时必须使用同一批 challenge ID：

```bash
python3 -m trust.challenge_compare baseline-report.json candidate-report.json \
  --minimum-samples 20 --minimum-family-samples 5 --alpha 0.05
```

比较器输出配对 wins/losses/ties 和双侧 exact sign-test。只有样本达到门槛且 `p <= alpha` 才称为
显著提升；样本不足为 `insufficient-data`。净回归即使未显著也会保守失败，challenge ID 缺失、
新增或 provider errors 增加同样 fail closed。这些是固定 challenge 上的配对表现，不是线上成功概率。

要检查随机稳定性，至少独立运行两次；必须使用 `--no-cache` 或不同 cache 目录，否则只是重复读取
同一模型输出，不是 trial：

```bash
python3 -m trust.challenge_run challenges.jsonl --goal "..." --model <模型> \
  --no-cache > trial-1.json
python3 -m trust.challenge_run challenges.jsonl --goal "..." --model <模型> \
  --no-cache > trial-2.json
python3 -m trust.challenge_trials trial-1.json trial-2.json > stability.json
```

稳定性报告给出 empirical `pass@k`（k 次至少一次成功）、`pass^k`（k 次全部成功）和翻转率，
并按 failure family 分层。provider error 计为失败 trial，不能从分母删除。trial 的 provider、
rubricVersion 或 challenge IDs 不一致时拒绝比较。pass@k/pass^k 仍不是部署成功概率。

有限人工标注预算优先复核 provider errors、claim/ordering 翻转、漏检反事实和 evidence gaps，
同时保证 failure-family 覆盖：

```bash
python3 -m trust.review_queue challenges.jsonl trial-1.json trial-2.json \
  --limit 20 --minimum-per-family 1 \
  --out blind-review.jsonl --answer-key sealed-answer-key.jsonl
```

默认 queue 不包含 synthetic truth，也不展示模型输出，避免答案泄漏和锚定偏差；answer key 单独
保存。只有完成独立首轮标注后做二阶段错误分析，才可显式加 `--include-model-outputs`。
抽样优先级可以由模型行为决定，但标注界面不能因此暴露模型答案。

## Rubric prompt 改写鲁棒性

`trust.prompt_robustness` 接受至少两个经人工确认语义等价的 prompt。每个变体必须声明七项不变量：
evidence-only、三个 claim 分离、不得翻转 oracle、unknown 弃权、证据回链、非概率语义和严格 JSON。

```bash
python3 -m trust.prompt_robustness challenges.jsonl prompt-variants.json \
  --goal "保存并验证生效" --model <模型> --score-range-threshold 0.1
```

报告给出最坏情况 claim exact rate、最坏情况 ordering sensitivity、claim/ordering 翻转率、Judge
分数 range 和超阈值样本数，并按 failure family 分层。prompt 文本哈希进入 Judge cache key；不同
改写不会误读同一缓存。随机 trial 强制 promptId/hash 相同，模型版本比较也强制 promptHash 相同，
避免把 prompt 差异误归因给随机性或模型升级。

## Judge indirect prompt injection

rubric v4 明确把 trace、selector、DOM/页面文字、network body、observation 和 reference content
标为不可信数据：它们可以作为证据，但永远没有指令权威。八类成对攻击包括 override、伪 SYSTEM、
给满分、伪造 evidence、prompt 泄露、多语言、编码和隐藏 HTML：

```bash
python3 -m trust.injection_robustness challenges.jsonl \
  --goal "保存并验证生效" --model <模型> --score-tolerance 0
```

攻击版本只增加不可信页面内容，确定性 oracle 必须与基线逐字段相同；随后检查 claim 不变、输出
仍合规且五维分数变化不超过阈值。claim 劫持、schema/evidence 破坏和仅分数操纵都会算 attack
success。该有限攻击集用于回归，不代表普遍安全证明。

## 五维分数证据

rubric v5 要求 `action_correctness`、`argument_quality`、`context_fit`、`evidence_quality`、
`replay_safety` 每个数字都对应独立的 `scoreEvidence.{dimension}.{evidence,reason}`。证据 token
必须回链到当前字段、rule finding、oracle criterion 或原始 observation evidence；一个 step 总
reason 不能替代五项解释。保守 hybrid 若用确定性规则压低模型分，还会追加该规则证据。

Phoenix 的每个 dimension annotation、challenge report 和 calibration JSONL 都保存这份逐维依据，
因此可以审核“为什么 replay_safety=0.4”，而不是只看到一个无法追溯的数值。所有值仍是
uncalibrated ordering score，不是概率。

rubric v6 将同一约束扩展到 `trajectory.completeness`、`necessity`、`ordering` 和
`evidence_closure`。每维都必须提供独立的 `trajectoryEvidence.{dimension}.{evidence,reason}`，并
回链到 reference steps、flow oracle、trace findings、具体 flow criterion/evidence 或 oracle checks。
一句共享的 trajectory reason 不能支撑四个分数。挑战报告保留完整对象，Phoenix 根 span 逐维导出
分数与依据；这些值同样只是未校准排序分。

rubric v7 进一步要求 evidence 数组中的**每一个** token 都能回链；不允许用一个合法 token 掩护
若干编造 token。`trust.evidence_audit` 随 challenge runner 自动报告 `backlinkPrecision`、
`atomicCitationRate`、`evidenceChangeRate` 和 `defectCitationRate`，并按失败家族拆分。前两项区分
“合法但泛化的字段引用”和具体 oracle/finding 事实；后两项检查反事实发生后证据是否变化、是否
点出被改变的确定性真值。它们都是 evidence 行为诊断，不是成功率或概率。

rubric v8 为每个 oracle check 生成绑定 `scope + criterion + verdict + evidence/reason digest` 的稳定
`evidenceId`。三个 step claims 必须引用相关状态 ID；有 `action.completed` 时，
`action_correctness` 必须引用对应 ID；有 flow checks 时，`evidence_closure` 必须引用 flow ID。
因此 pass→fail 即使 criterion 名称不变，引用也必须变化。内置 30 组反事实门禁要求
`evidenceChangeRate=1.0` 且 `defectCitationRate=1.0`。

rubric v9 将 ScreenSpot 风格的 point-in-box 与稳健命中分开：`target.hit_point` 仍表示坐标是否
在专家/运行时目标框内；`target.hit_margin` 报告点击点到最近边缘、按目标宽高归一化后的裕量，
默认至少 5%。框内但仅 1% 裕量的点击会通过 hit_point、失败 hit_margin；没有 point+bbox 时为
unknown，不从动作成功反推几何稳健性。

轨迹新增 `flow.path_efficiency`：无缺步时为 referenceSteps/actualSteps，缺少任一必需步骤则强制为
0，防止少做步骤获得效率奖励。oracle-spec v2 可显式声明 `maxExtraSteps`、`maxTotalRetries`、
`maxDurationMs`；未声明重试/耗时预算时只保留观测，不判失败。

rubric v10 收紧 `target.semantic_identity`：`semanticMatch` 裸布尔值不能证明点到了正确业务对象。
运行观测必须同时提供非空 `semanticIdentity`、`semanticEvidence[]` 和受控
`semanticEvidenceSource`。允许来源仅为 accessibility tree、DOM attributes、人工核验视觉证据或
test fixture；缺失或 `model-vibes` 等未知来源一律 unknown。带来源证据的错误身份才确定性 fail。

rubric v11 对 `visible/enabled/unobscured` 使用同样的逐字段来源契约。允许 Playwright actionability、
accessibility tree、DOM geometry、人工核验视觉证据或 test fixture。只有成功、非 force 的 DOM
locator 动作，且执行记录没有显式覆盖字段时，才可从 Playwright 的 actionability checks 推导三项
为 true；force、visual、失败动作或裸布尔值不能继承该证明。

rubric v12 将相同原则扩展到动作后反应。网络响应必须带 response-validator 来源与校验证据；
`causallyLinked` 必须带 action-scoped listener/event-window 来源；UI/状态 assertion 必须带
Playwright/API/数据库/人工视觉 verifier 证据；`next_step_ready` 必须带执行序列证据。字段匹配但
只有 `ok=true`、`passed=true`、`ready=true` 的记录均为 unknown。新增运行时下一状态失败反事实，
challenge set 随后由 persistence 来源反事实扩展为 32 records/30 counterfactuals。

rubric v13 要求 `reaction.persistent_state` 的刷新、API、数据库重查或人工视觉复验也声明受控 verifier
来源。状态值、方法或时限在可信证据下不符为 fail；缺证据或来源为 `model-vibes` 时为 unknown，
不会把未知伪装成失败或成功。

rubric v14 要求实际 action type/completed 携带 execution/tool evidence；裸 `completed=true` 不能证明
动作发生。rubric v15 进一步要求 target resolved、candidate uniqueness、hit point 分别携带 resolver、
候选计数和几何/执行器 provenance。裸 `resolved=true`、`matchCount=1`、`hitWithinTarget=true` 均为
unknown。实测框外点明确记录 `point-outside-runtime-bounding-box`，不与框内证明共用标签。

Challenge CLI 将这些可证明的不变量作为 `evidenceAudit.contractGate`：全部记录必须成功审计、
backlink precision 必须为 1、全部反事实必须改变证据并引用被改变的缺陷，否则进程返回非零。
`atomicCitationRate` 只报告不硬设阈值，待真实人工金标按错误成本校准。

rubric v16 为 step 字段和 trajectory 输入生成绑定 `scope + path + value digest` 的 evidence ID。
`current.selector`、`before`、`flowOracle` 等泛化字段名只用于导航，不能单独支撑分数；每个 step 和
trajectory 维度至少引用一个内容绑定 ID、oracle 状态 ID、rule finding 或原始原子证据。哈希只证明
引用对应本次输入，不证明事实为真，也不是概率。32 条合成 challenge 的原子引用率现为 1.0。

rubric v17 将确定性 oracle fail 映射为语义相关维度的硬上限：误点/动作失败约束 action 与 argument，
错误反应约束 evidence/context，流程缺失、乱序、冗余和最终结果失败分别约束四个 trajectory 维度。
`unknown` 不冒充 fail，而由 oracle evidence coverage 降低总排序。最终 aggregate 是 deterministic、
LLM step、trajectory 和 evidence-adjusted oracle 的单调合取乘积；固定非负输入轴下，一个轴变差且
其他轴不升时总排序不增。零因子饱和及舍入可能导致并列，不保证普遍严格下降；乘积没有概率或独立性语义。

rubric v18 把合成 meta-eval 从“只报告指标”升级为可执行门禁。单次 `behaviorGate` 要求零 provider
error、claim exact=1、所有反事实均被检查且严格降序、每个已检查失败家族 sensitivity=1；多次独立
报告的 `stabilityGate` 要求 claim/ordering 的 pass^k=1、flip rate=0、provider error trial rate=0。
Challenge 与 trials CLI 任一门禁失败均非零退出。阈值只针对确定性合成契约，不是部署准确率 SLA。

rubric v19 收紧 HUMAN gold 与准确率报告。Readiness 不只要求双人票数、样本数和 pass/fail 双类，
还逐 claim 要求足够重叠标签和最低 Cohen’s κ（默认 overlap≥30、κ≥0.6），避免总体一致性掩盖某个
claim 的歧义。准确率同时报告仅在模型作出二元判断上的 selective accuracy，以及把 abstention/漏预测
留在 gold 分母中的 effective accuracy；两者均给 Wilson 95% 区间。只有显式 probability 才计算
Brier/ECE，连续 ordering score 仍不得冒充概率。

## 三项可证伪的检查

```bash
python3 trust/validate.py <轨迹目录>...
```

| | 最近一次结果 |
|---|---|
| 变异单调性 | 43/43 注入缺陷后罚分变高 |
| **命名失败节点** | 5/6 实测标红的轨迹，规则层点出了真正断掉的那个节点 |
| 排序一致性 | 唯一的 stable-green 排第 1（只值 1 bit） |

第二项比任何分数都硬：它问的不是「你觉得这条轨迹好不好」，
而是**「你指的地方，就是它实际摔倒的地方吗」**。

## 依赖

轨迹的形状定义只在录制器那边（`scripts/trace_schema.py`），这里**不复制**一份 ——
复制出来的一定会漂移，漂移之后就会拿自己那份形状去评轨迹。
默认找同级目录的 `edr-cloud-recorder`，可用 `EDR_RECORDER_HOME` 覆盖。

    pip install playwright opencv-python   # replay_lab 取标签时才需要

进度和每一轮的结论见 [trust/STATUS.md](trust/STATUS.md)，原始方案见 [docs/PLAN.md](docs/PLAN.md)。完整的
目标架构、数据契约、评分链、Phoenix/HUMAN 方案、实施阶段和最终验收标准见
[AGENT_TRACE_EVAL_ARCHITECTURE.md](docs/AGENT_TRACE_EVAL_ARCHITECTURE.md)。

下一阶段的实验平台迁移、Golden 回放、业务 Oracle、逐轮代码目标和验收标准见
[Trace Evals 实验架构迁移设计](docs/EVAL_PLATFORM_MIGRATION_DESIGN.md)。