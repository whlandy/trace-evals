# 可信度评估 —— 进度

方案见 [docs/PLAN.md](../docs/PLAN.md)。每轮只闭环一件事，
结论必须带数字。**没有新证据不许改权重。**

## 当前阶段：原 P0–P3 + 集成完成；Trace step eval 增强的 M1 已完成

## M1 —— 逐步骤确定性评分骨架（2026-08-30）

设计见 [`TRACE_STEP_EVAL_DESIGN.md`](../docs/TRACE_STEP_EVAL_DESIGN.md)。借鉴 LangChain
AgentEvals 的 deterministic match / LLM-as-judge 分层，以及 OpenAI Graders 的
score/label/multi 组合形态，但保留本项目“未经校准不称概率”的边界。

产出：`trust/eval_types.py`、`trust/step_score.py`、
`test/test_trust_step_score.py`（5 条）。每个节点输出 action correctness、argument
quality、context fit、evidence quality、replay safety 五维分数，并保留原始 finding、
失败形态、grader 版本和 `deterministic` 置信标记。轨迹聚合同时给 mean、minimum、
bottom-k mean，避免一个致命步骤被大量正常步骤平均掉。

验收：新增 5 条测试通过；全仓库 **84 passed**。`blind_toggle` 只降低命中节点的
replay/context 两维；`no_assertions` 等整条轨迹 finding 保持 trace 级，不伪造节点归因；
schema 会拒绝越界分数。输出仍明确标记 `uncalibrated-ordering-only`。

## M2–M4 —— 轨迹匹配、模型 Judge、保守聚合与 meta-eval（2026-08-30）

产出：

- `trust/trajectory_match.py`：strict/unordered/subset/superset，另报缺步、多步、
  顺序漂移、参数不匹配和部分得分；
- `trust/judge.py`、`trust/providers/`：版本化 rubric、严格 JSON Schema、OpenAI
  Responses provider、输入哈希缓存；
- `trust/hybrid.py`：逐维取规则/model 较低值，silent-pass 上限 0.49、loud-later
  上限 0.74，模型不能翻转硬规则；
- `trust/evaluate.py`：规则、judge、hybrid、可选四模式参考匹配的统一入口；
- `trust/judge_validate.py`：结构有效性、mutation 敏感性、重复一致性、规则冲突、
  失败节点命中和校准 JSONL 导出。

验收：全仓库 **99 passed**，`compileall` 与 `git diff --check` 通过。测试 provider 下
全部可注入 mutation 的 hybrid 排序值严格下降，保守合并后的规则冲突为 0；固定输入的
标签一致率为 1。OpenAI provider 的请求形状经无网络 mock 验证为 strict JSON Schema。
真实模型质量仍须在指定模型/API key 和新增盲评样本上运行 meta-eval；在此之前所有模型
与 hybrid 输出均为 `uncalibrated-judge-score`，不宣称准确率或校准概率。

## Phoenix 集成（2026-08-30）

采用 Phoenix 作为自托管 trace/annotation/dataset/experiment 外层，现有 `trust/*` 保持
评分事实来源。`trust/phoenix_export.py` 将根 trace 映射为 CHAIN span、每个节点映射为
TOOL 子 span，并把五维分数与 overall 批量写成 Phoenix span annotations。

本地部署使用 `docker-compose.phoenix.yml`，SQLite 数据写入 Docker volume，并设置
`PHOENIX_ALLOW_EXTERNAL_RESOURCES=false`，内网 UI 不请求外部字体等资源。Python 导出依赖
单列在 `requirements-phoenix.txt`，未安装时不影响核心评分和测试。

导出器采用 Phoenix 2026 当前接口：`phoenix.otel.register()` 发送 OTLP spans，先
`force_flush()`，再用 `client.spans.log_span_annotations(sync=True)` 写 annotations。
新增 3 项测试覆盖完整节点/五维映射、非概率语义 metadata、flush 后写 annotation 的顺序。

## Action—Reaction—Flow Oracle 与 Judge v2（2026-08-30）

细则见 `../docs/AGENT_EVAL_RUBRIC.md`。依据 Anthropic 的 transcript/outcome 分离、LangChain 的
single-step/trajectory 分层和 WebArena 的 functional correctness，新增 `trust/oracle.py`：
分别验证运行时目标是否 resolved/unique、点击是否落框、动作是否完成、声明的网络响应/断言
是否真实出现，以及整条执行和测试用例在必需步骤、额外步骤、顺序、参数上的一致性。

所有原子检查返回 pass/fail/unknown/not_applicable；unknown 的 score 为 null，不用 0.5
掩盖观测不足，并单独计算 evidenceCoverage。Judge rubric 升级为
`trace-action-reaction-flow-v2`，每一步强制输出 target_execution、post_action_reaction、
testcase_conformance 三项 claim。代码会复核 claim，模型若把缺少观测的 unknown 改成 pass，
评估直接失败。claims 同步导出为 Phoenix span annotations。

## Execution observations 与反事实（2026-08-30）

新增 `trust/observations.py`，将 `edr.execution-trace/v1` 的每一步转换为 target/action/reaction
证据。DOM/visual 动作成功可证明点击落在运行时选中的目标上，但不能冒充“业务对象语义正确”；
网络 `ok=true` 只在上游已校验 status、request body、response body 后产生；断言步骤的 success
转换为真实 assertion pass。相邻步骤的实际执行状态形成 `reaction.next_step_ready`，用于判断
点击后是否进入测试用例要求的下一状态。`replay_lab.py` 现在把 observations 写进每次 run。

新增 `trust/observation_mutate.py` 的 7 类反事实：误点、歧义目标、未定位、动作失败、无网络
反应、500 响应、断言失败。**7/7** 都使 evidence-adjusted ordering score 严格下降；删除
reaction 只会变成 unknown 并降低 evidenceCoverage，不能通过删除失败证据抬分。该指标仍明确
标记为 `ordering-only-not-a-probability`。统一 evaluate 输出携带完整 oracle，原子检查和三项
claims 均导出到 Phoenix。全仓库 **133 passed**。

## P0 —— 特征抽取与语料盘点（2026-08-28）

产出：`trust/features.py`（只抽事实，不打分）、`trust/inventory.json`、
`test/test_trust_features.py`（11 条，逐条守一个识别能力）。

验收：抽掉「盲开关」和「同义反复」两个判据后，自检从 11 passed 变成 3 failed
—— 测试确实守得住，不是必然通过的摆设。

抽取器只经由 `trace_schema` 的访问器读轨迹，不自己按字段路径挖 `attach.*`。
这一轮正好撞上格式从 v1 换到 v2，第一版按字段路径写的代码当场作废 ——
形状定义只该有一处。

### 语料盘点（7 条，均已按 v2 重新生成）

| 轨迹 | 步 | 断言 | 同义反复 | 仅存在性 | 盲开关 | 位置选择器 | 写请求 | 可选 |
|---|---|---|---|---|---|---|---|---|
| maa-flow | 9 | 0 | 0 | 0 | 0 | 1 | 1 | 0 |
| maa-flow2 | 6 | 0 | 0 | 0 | 0 | 1 | 1 | 0 |
| maa-flow3 | 13 | 0 | 0 | 0 | 0 | 3 | 2 | 0 |
| maa-flow4 | 15 | 0 | 0 | 0 | 0 | 1 | 3 | 0 |
| maa-flow5 | 17 | 0 | 0 | 0 | **4** | 1 | 2 | 0 |
| maa-flow6 | 9 | 0 | 0 | 0 | 0 | 0 | 1 | 1 |
| maa-flow7 | 9 | 2 | 0 | **2** | 0 | 0 | **12** | 1 |

模板覆盖全部 1.0。

### 四个结论

**1. 抽取器和已知事实对得上（这是 P0 的验收点）。**
maa-flow5 盲开关 = 4，正是当初查出来那 4 步；maa-flow6 = 0，而它是连跑三遍
score=100 且分数完全一致的那条。不是「跑通了」，是**量出来的数和独立获得的
结论一致**。

**2. 语料里没有一条断言是「实打实的命题」。**
7 条轨迹里 5 条**一条断言都没有**；仅有的 2 条是「至少有一个可见」——
由同义反复自动改写而来（v1 时这 2 条还是 `to_have_text` 的同义反复形态）。
存在性断言是真命题，但它只能证明「这段文字还在」。
**证据力这根轴目前近乎为零，不是个别轨迹的问题。**

**3. 写请求普遍存在，最多一条 12 个。** 每条轨迹都会改动自己下一次回放的
起点 —— P1 的「第二遍」不是重复实验，是换了初始条件的新实验。
maa-flow5 的振荡正是如此。

**4. 老轨迹缺的是**证据**，不是格式，重新生成补不回来。**
上游把「选择器落到 CSS 兜底 → 可选」这条代理判据删掉了（理由正确：
选择器形态不是可选语义，一律跳过会让轨迹少做一步仍报成功）。
现在的判据是录制时观察到的事实 `dismissesOverlay`。
后果：5 条老轨迹（录制时还没有这个观察器）**可选标记全部归零**，
而它们仍各自带着 1~3 个 CSS 路径步骤 —— 那些多半就是关弹窗。
这类轨迹**只能重录，不能重生成**：观察器没跑过，事实就不存在。

→ 可信度必须把「用哪一版录制器录的」算进观测充分性。

### 顺带发现：仓库自带的两条样例已经加载不了

`recordings/tiangong-hisec-readonly` 和 `recordings/tiangonglab-8000-recording`
的 trace.json 仍是 v1，`load_trace` 直接报「轨迹缺少 $meta 节点」。
而且这两个目录**没有 recording.json**，重新生成也做不到 —— 只能重录或删掉。
它们暂时不进语料。

### 顺带发现：轨迹丢掉了歧义事实

`recording.json` 里有 `ambiguous` / `matches`（录制器验过唯一性），
但 `_selector()` 不搬它。现在只能从 `sel` 反推 `.first()` 这个症状，
拿不到「到底撞了几个」。P2 决定：反推，还是让轨迹带上它。

## P2a —— 变异器（2026-08-28，第 2 轮）

产出：`trust/mutate.py`（10 种在册缺陷的注入器）、`test/test_trust_mutate.py`（31 条）。

真实语料只有 7 条，训不出也验不了模型。但每一种缺陷都真实发生过、且我们知道
它的正确标签 —— 所以拿好轨迹成对造出 (原体, 变异体)。这既是 P3 的训练/验证
数据，也是评估器自己的回归测试。

每种变异都**声明**它必须让哪个汇总特征往哪个方向动。这张表是可证伪的：
特征没动就说明抽取器对那种缺陷是瞎的。

### 在 7 条真实语料上跑的结果：44 处可注入，漏检 0

| 变异 | 可下手的轨迹数 | 说明 |
|---|---|---|
| positional_selector / volatile_anchor / first_of_many | 7 | 每条轨迹都能下手 |
| drop_expected_body / drop_template | 7 | 同上 |
| blind_toggle / drop_switch_carrier | 2 | 只有 flow5、flow6 有真正的 SetSwitch |
| drop_optional | 2 | 只有 flow6、flow7 有可选标记（呼应 P0 结论 4） |
| tautological_assertion / drop_assertions | 1 | **只有 flow7 有断言** |

最后一行是 P0 结论 2 的另一种呈现：**语料里几乎没有断言可供破坏**。
证据力这根轴不仅分数低，而且连负样本都造不出来 —— P3 想校准「证据力」
这一维，先得有断言才行。

验收：把 `blind_toggle` 改成「返回描述但什么都不改」，
`test_every_mutation_moves_its_declared_feature[blind_toggle]` 立刻变红。
另外两条硬性质也守着：每种变异都必须能在参考轨迹上下手（新加变异忘了扩充
夹具会立刻喊出来）；变异只能改深拷贝，改到原体上就会污染语料和后续对照。

## P2b —— 规则层（2026-08-28，第 3 轮）

产出：`trust/rules.py`（9 条规则）、`trust/findings.json`、
`test/test_trust_rules.py`（16 条）。仍然**不打分**。

不给「严重度」是刻意的：那是伪装成事实的权重，而权重要等 P1 的标签校准。
取而代之的是一个真正的事实维度 —— **这个缺陷失败时是什么形态**：

| | 含义 |
|---|---|
| `silent-pass` | 悄悄绿：事情做错了，回放照样报成功。最坏的一类 |
| `flaky` | 时好时坏：换个起点结果就变 |
| `loud-later` | 当时不报，以后才炸：录完当场全绿，隔几小时就红 |
| `weak` | 不会失败，但它绿了也说明不了什么 |

每条发现项都带 evidence（从轨迹里读到的原话）和 consequence（会发生什么）。
**分数会被优化，证据不会** —— 证据是这一层的主产物，计数只是摘要。

### 全语料 41 条发现，其中 silent-pass 11 条

| 轨迹 | 步 | 发现 | silent-pass | flaky | loud-later | weak |
|---|---|---|---|---|---|---|
| maa-flow | 9 | 4 | 1 | 1 | 2 | 0 |
| maa-flow2 | 6 | 4 | 1 | 1 | 2 | 0 |
| maa-flow3 | 13 | 7 | 1 | 2 | 4 | 0 |
| maa-flow4 | 15 | 6 | 1 | 3 | 2 | 0 |
| maa-flow5 | 17 | 9 | **5** | 2 | 2 | 0 |
| maa-flow6 | 9 | **2** | 1 | 1 | 0 | 0 |
| maa-flow7 | 9 | 9 | 1 | 5 | 1 | 2 |

规则命中：mutates_state 15、positional_selector 7、no_assertions 6、
recorder_predates_overlay_evidence 5、blind_toggle 4、existence_only 2、
unmarked_dismissal 1、first_of_many 1。

### 三个结论

**1. 规则层独立重新发现了一个已知为真的缺陷。**
`unmarked_dismissal` 指向 maa-flow7 的 `step_0002` —— 那正是「系统检测到您未绑定
手机号码」提示条的关闭图标，两轮前手工诊断出的同一个问题。规则不知道那段历史，
是从命名和标记缺失推出来的。**这是规则层目前最有力的验证。**

**2. 排序和实测吻合，但样本只有 1。**
发现最少的是 maa-flow6（2 条），而它正是实测连跑三遍分数完全一致的那条；
silent-pass 最多的是 maa-flow5（5 条），正是在 9/17 和 15/17 之间振荡的那条。
方向对得上 —— 但这是 n=1 的巧合级证据，**不能当成校准**。P1 的标签才算数。

**3. 有一类缺陷在产物里根本不可见。**
`test_every_mutation_produces_a_new_finding` 一上来就把 `drop_optional` 抓红了：
标记被摘掉之后，轨迹从表面上看是干净的。只能靠间接症状（看着像关闭图标的点击
却是必经节点）去认，判据是命名习惯 —— 会漏（带文字的「我知道了」按钮就漏），
但不会乱指：真正的业务图标不会叫 close。

→ 记给 P3：有些缺陷需要拿 `recording.json` 和 trace 对照才看得见，
光看轨迹是看不出来的。

## P2c —— recording ↔ trace 对照（2026-08-28，第 4 轮）

产出：`trust/crosscheck.py`（3 条规则）、`trust/crosscheck.json`、
`test/test_trust_crosscheck.py`（5 条）。

起因是第 3 轮的发现：有一类缺陷在产物里根本不可见。录制里有的事实，编译进
轨迹时可能丢掉 —— 只看轨迹就永远发现不了「丢了什么」。对照靠
`provenance.sourceStepId` 把节点连回录制步骤。

全语料结果：**1 条发现**。
`maa-flow7 step_0004`：录制记着 `ambiguous / matches=2`，轨迹里没有
（正是 P0 顺带发现的那条，现在变成可自动检出的规则）。

两件**没有**发生的事同样是结论：0 处中段掉步（编译没有悄悄丢步骤），
0 处凭据外泄（secret 步骤都被正确切掉了）。

自检里最要紧的是一条**否定**用例：登录前缀是编译时故意切掉的，不能报成掉步 ——
否则每条带登录的轨迹都会被冤枉，真正的掉步就淹没在噪音里。

## P1 —— 重复回放取标签（2026-08-28，第 5 轮）

产出：`trust/replay_lab.py`、`trust/labels.jsonl`。7 条轨迹各跑 2 遍，
共 14 次回放。跑之前先登录并导出登录态 —— 会话中途失效会把每条轨迹都变成红，
那种红和轨迹无关，**当成标签就是在伪造真值**（所以有 `invalid` 这一类）。

| 轨迹 | 标签 | 断在 | 那一步是什么 |
|---|---|---|---|
| maa-flow6 | **stable-green** | — | 两遍都 100.0 |
| maa-flow | stable-red | step_0002 | 提示条关闭（未标可选） |
| maa-flow3 | stable-red | step_0002 | 提示条关闭（未标可选） |
| maa-flow7 | stable-red | step_0002 | 提示条关闭（未标可选） |
| maa-flow2 | stable-red | step_0004 | `getByText("DESKTOP-…-192.0.2.10")` |
| maa-flow4 | stable-red | step_0004 | 同上 |
| maa-flow5 | stable-red | step_0007 | `getByPlaceholder("1-59")` |

### 三个结论

**1. 我在方案里立的对照，一半没兑现 —— 这要说清楚。**
预期是「maa-flow5 必须被标成翻转」。实测是 stable-red。那次振荡观察的是 v1 的
产物、而且环境状态早已不同，**那条对照今天已经作废，不是被证实**。
另一条对照成立：maa-flow6 = stable-green。

**2. 6 条红不是 6 个原因，是 3 类，其中最大一类恰好是规则层已经报过的。**
3 条断在同一条「未绑定手机号」提示条上 —— 那正是
`unmarked_dismissal` / `recorder_predates_overlay_evidence` 指着的地方。

**3. 标签买来了一条新规则。** 2 条断在 `getByText("DESKTOP-…-192.0.2.10")`：
主机名/IP 是**这套环境此刻的库存**，不是界面文案，和时间戳同类、只是变得慢。
加了 `environment_data_anchor`。

但它在这份语料上**不区分好坏** —— 7 条全命中，包括那条 stable-green
（maa-flow6 也按主机名选资产，只是那台资产今天还在）。
真正把绿和红分开的是浮层步骤。这正是不能手工配权重的理由。

## P3 —— 打分与 meta-eval（2026-08-28，第 6 轮）

产出：`trust/score.py`、`trust/validate.py`、`test/test_trust_score.py`（14 条）。

罚分按**失败形态**给，不按规则给：悄悄绿 25 / 以后才炸 15 / 时好时坏 10 /
说明不了什么 5。**这四个数字是拍脑袋的，只有相对顺序有依据**，
所以每份报告都带 `uncalibrated-ordering-only`：只承诺排序，不承诺概率。
标签是 1 绿 6 红，这个比例算不出校准误差 —— 方案里要的 ECE ≤ 0.1 **做不到**，
不是没做，是数据不允许。

### 三项可证伪的检查

| 检查 | 结果 |
|---|---|
| ① 变异单调性 | **43/43** 对样本注入缺陷后罚分变高 |
| ② 命名失败节点 | **5/6** 实测标红的轨迹，规则层点出了真正断掉的那个节点 |
| ③ 排序一致性 | 唯一的 stable-green 排第 1（只值 1 bit，不能当校准） |

② 比任何分数都硬：它问的不是「你觉得这条轨迹好不好」，而是
**「你指的地方，就是它实际摔倒的地方吗」**。唯一没命中的是 maa-flow5 的
`getByPlaceholder("1-59")` —— 那是「表单面板没打开所以字段不在」，
属于上下文依赖，现有规则看不见。

用罚分而不是分数做单调性检验：分数有 0 下限，坏透了的轨迹再注入也还是 0，
那是量尺到头，不是评估器没抓住。

实测中写错过一次并被自己的测试抓住：`hit` 把所有行都算成命中，5/6 报成 6/6。
`test_named_the_failure_only_counts_real_hits` 现在守着它。

## P5 —— 集成（2026-08-28，第 7 轮）

`python3 trust/audit.py <轨迹目录>` 一条命令出体检报告：分数 + 逐条证据，
按三根轴分栏，带实测标签。SKILL.md 的工作流里加了第 6 步「Audit What the Green
Actually Proves」。

样例（maa-flow6，回放满分、实测 stable-green）：

    ══ maa-flow6 ══  9 步 · 分数 50（罚分 50）
       uncalibrated-ordering-only —— 分数只表达排序，不是概率
       实测标签：stable-green
       ── 证据力（1 条）──
       [悄悄绿] 整条轨迹：9 步，0 条断言
           → 这条轨迹只证明「这串操作能走完」，不证明系统做对了任何事

**这就是整个项目要说的那句话**：回放 100 分的轨迹，可信度 50。

## 剩下的：都需要新证据，不是写代码能推进的

- **标签太少且一边倒**（1 绿 6 红，0 翻转）。校准需要一批稳定绿和翻转样本，
  而现在的语料造不出来 —— 6 条老轨迹本身就是坏的。
  **要推进得先重录几条**（重录还能顺带补上浮层观察这类只能在录制时取得的证据）。
- **证据力那根轴连负样本都造不出来**：全语料只有 1 条轨迹有断言。
  想校准这一维，先得有断言。
- **L3（LLM 评审）没做**。规则层目前 5/6 的命名率，剩下那 1 条是上下文依赖，
  正是 LLM 评审的用武之地；但在标签这么少的时候上它，无法验证它是不是在瞎说。

## Phoenix + 动作—反应—流程 Judge（2026-08-30）

新增版本化 LLM judge、确定性 oracle、Phoenix span/annotation 导出和运行观测转换。
逐步判断被拆成三个独立 claim：`target_execution`（是否点到）、
`post_action_reaction`（点击后是否出现预期网络/UI/状态反应）、
`testcase_conformance`（整条流程是否符合测试用例）。缺少运行事实时必须输出 `unknown`，
模型不得翻转确定性 oracle。当前 11/11 个运行观测反事实变异都会降低覆盖率调整后的排序值。

## 人工盲标与校准门控（2026-08-30）

新增 `trust/calibrate.py`：

- claim 级人工标签必须携带 evidence；至少两位独立标注者，严格多数才形成 gold；
- 平票/人数不足进入仲裁，同一标注者重复提交不能增加票数；
- 报告两两 Cohen's kappa，以及三个 claim 各自的 precision/recall/F1；
- `unknown` / `not_applicable` 作为弃权，单独报告 coverage，不能靠少答题刷准确率；
- 只有显式 probability、有效样本至少 30 且正负类均存在，才输出 Brier/ECE；所有现有
  ordering score 继续明确标为非概率。

当前全套测试 **147/147** 通过。这里证明的是评估框架的结构约束、反事实敏感性和指标
计算正确；尚未证明某个真实 Judge 模型的准确率。下一步仍需双人盲标数据和真实模型输出。

## Phoenix golden dataset 与实验回归门（2026-08-30）

新增 `trust/phoenix_dataset.py`，按 Phoenix 官方的 input / reference output / metadata
契约生成数据集。三个 claim 不再被压成一个总分，而是各自成为独立 example；稳定 ID、
case、nodeId、rubricVersion、标注者和投票来源都随数据保存。只要存在未仲裁标签，上传就
fail closed。

新增 baseline/candidate Judge 配对比较：逐 claim 检查 F1 与 coverage，任一退化即失败。
实现时测试发现旧指标只遍历预测行，导致 Judge 漏掉 gold 时样本会从分母消失；现已改成
由 gold 决定分母，缺失预测计入 abstention。空预测不再可能通过回归门。

## GUI grounding 与点击 actionability（2026-08-30）

参考 ScreenSpot/SeeClick 的 point-in-target-bounding-box click accuracy，把点击证据细化为：
resolved、unique、hit_point、visible、enabled、unobscured、semantic_identity。几何命中、
可操作性和业务语义互相独立，任一失败都不能被“动作执行成功”掩盖。

`trust/observations.py` 在 execution 同时提供 point/boundingBox 时直接计算几何命中，并把
证据标记为 `measured`；旧执行器只给成功回执时保留 `executor-derived`。后四项缺失时明确
为 unknown。反事实集合新增隐藏、禁用、遮挡和错误语义目标，当前 **11/11** observation
mutation 都会降低 evidence-adjusted ordering score。

## 动作—反应因果闭环与最终 outcome（2026-08-30）

新增 `reaction.causal_attribution`：响应内容匹配与因果归属分开判定。replay 执行器在动作前
建立 response waiter，因此同一 execution step 捕获的响应被标记为 action-scoped；普通外部
observation 没有归因字段时保持 unknown。后台请求即使 URL/status/body 完全匹配，也不能证明
点击产生了副作用。

响应匹配从 `all(any(...))` 改成一对一消费，堵住“一条实际响应同时满足两条相同期望”的漏洞。
新增 `reaction.downstream_assertion`，把紧随动作的 Assert 运行结果连回前一步；新增
`flow.reaction_coverage`、`flow.causal_coherence` 和 `flow.final_outcome`。没有任何 outcome
oracle 时最终结果固定为 unknown，不能因步骤全部执行完就判业务成功。

反事实集合新增无因果网络响应和后续断言失败，当前 **13/13** observation mutations 均降低
评分。全套测试 **161/161** 通过。

## 持久业务状态 oracle（2026-08-30）

新增独立 `trace-eval.oracle-spec/v1`，不污染 recorder 的严格 trace schema。测试作者可按 node
声明 persistence method（reload/requery/api/database/new_session）、expected 和 maxDelayMs。
对应 observation 必须提供 method、passed、observed、delayMs 和非空 evidence。

新增 `reaction.persistent_state`：缺观测为 unknown；值不符、复验方式偷换、超时或没有证据
均为 fail。规格经过严格字段、schema、nodeId 和 method 校验，并完整进入 Judge payload/cache
key 及统一 evaluate CLI。提供 `oracle-spec.example.json`。新增 7 条专项测试，全套测试
**169/169** 通过。

## Oracle spec → Phoenix dataset 版本闭环（2026-08-30）

`trust/phoenix_dataset.py` 现在接受每个 case 对应的 `--oracle-spec CASE FILE`。相关 node 的
持久状态规格和展开后的 atomic oracle checks 进入 experiment input，完整 spec 的 SHA-256
进入 metadata，避免不同判据的结果被错误横向比较。

同时修正了一个可复现性缺口：此前虽然计算了稳定 example ID，但上传时只把它放进 metadata，
Phoenix 并不会据此去重。现在按官方 DataFrame + `example_id_key` 接口做版本化 diff；同一
case/node/claim 会稳定更新，缺失的旧 example 按 Phoenix full-replace 语义从新版本移除。
Phoenix 可选依赖新增 pandas。全套测试 **171/171** 通过。

## 分层固定 challenge set（2026-08-30）

新增 `trust/challenge_set.py`，从健康参考轨迹生成 2 个 control + 27 个 counterfactual：13 个
运行观测失败、10 个静态/轨迹缺陷、4 个持久状态失败。每条 JSONL 都自包含输入、reference、
observations、oracle spec、step claims、flow verdicts、规则结果及相对 baseline 的 changed checks。

自检最初发现 `drop_template` 不会降低轨迹 oracle 分——这是正确的职责边界，因为删视觉回退
不改变 action transcript。challenge 随后改为同时携带 oracle truth 和 deterministic rule truth，
并要求每个反事实至少由正确层捕获；不能为了统一数字让 oracle 越权猜测静态可回放性。

所有 challenge ID 稳定，所有 27 个反事实均有证据变化且至少一个确定性排序下降。合成标签明确
标记为 `deterministic-synthetic-counterfactual`，不是人工 gold，也不是校准概率。全套测试
**174/174** 通过。

## Judge challenge runner 与证据回链（2026-08-30）

新增 `trust/challenge_run.py`，可对任意 provider 批量运行 29 条 challenge，并按 failure family
报告 claim exact accuracy、相对 control 的 hybrid ordering sensitivity、provider errors 和每条
差异。单个 provider 错误被隔离，不会中止整批，防止只保留成功样本造成幸存者偏差。

用只看静态 findings 的测试 provider 运行后，claim 因硬 oracle 契约保持一致，但 grounding 家族
ordering sensitivity 明确为 0；这证明分层指标能暴露“输出格式正确但模型分数没看懂运行证据”。

同时将 rubric 升级到 `trace-action-reaction-flow-v3`：claim evidence 不仅要求非空，还必须精确
回链到相关 oracle criterion、原始 evidence token 或当前 nodeId。空数组和 `looks-right` 之类
虚构依据均 fail closed，rubric 版本变化会自然生成新 cache key。全套测试 **177/177** 通过。

## 配对显著性与最小样本门控（2026-08-30）

新增 `trust/challenge_compare.py`。baseline/candidate 必须使用完全相同的 challenge IDs，并按记录
比较 claim record exactness 和 counterfactual ordering detection。报告 wins/losses/ties、rate delta
和双侧 exact sign-test p-value，同时按 failure family 分层。

提升只有在样本量达到门槛且 `p <= alpha` 时标记 significant；不足时明确
`insufficient-data`。发布策略有意非对称：净回归即使尚未显著也 fail，提升则必须有显著证据。
challenge 集变化或 provider errors 增加也 fail closed。测试验证 6 胜 0 负时 p=0.03125，而
4 胜 0 负在 minimum=5 时仍拒绝下结论。全套测试 **181/181** 通过。

## 多 trial 稳定性（2026-08-30）

新增 `trust/challenge_trials.py`，强制至少两份 report 且 provider、rubricVersion、challenge IDs
一致。逐 challenge 和逐 failure family 报告 empirical claim/ordering pass@k、pass^k、flip rate
及 provider-error trial rate。错误调用作为失败 trial 留在分母。

`challenge_run` 新增 `--no-cache`；独立 trial 必须禁用 cache 或使用不同 cache 目录，避免把同一
响应的重复读取伪装成模型稳定性。专项测试构造了 `pass@3=1.0` 但 `pass^3=0.0`、flipRate=1.0
的模型，证明“偶尔答对”和“每次可靠”被明确分开。全套测试 **184/184** 通过。

## 分层人工复核队列（2026-08-30）

新增 `trust/review_queue.py`。优先级依次吸收 provider error、claim mismatch/flip、ordering flip、
counterfactual 漏检和 evidence gap，并先按 failure family 取最低配额，再用全局优先级填满预算。
report/challenge IDs 不一致时拒绝抽样。

队列与 answer key 分文件输出。实现初版虽隐藏 synthetic expected，却展示了模型输出；测试审视后
确认这仍会造成锚定偏差，已改为默认同时隐藏真值和模型答案。`--include-model-outputs` 只允许在
首轮独立标签完成后的二阶段错误分析中使用。全套测试 **188/188** 通过。

## Rubric prompt 改写鲁棒性（2026-08-30）

新增 `trust/prompt_robustness.py`。prompt bundle 至少包含两个唯一文本，并要求人工确认语义等价、
逐项声明 evidence-only、separate-claims、oracle-non-override、unknown-abstention、
evidence-backlink、non-probability、strict-json 七项不变量；缺一项或未认证即拒绝运行。

每个变体在同一 challenge set 和 provider 上运行，报告 worst-case claim exactness、worst-case
ordering sensitivity、claim/ordering flip rate、mean Judge score range、range exceedances 和
provider errors，并按 failure family 分层。测试 provider 的硬 claims 可保持不变，但仅因 prompt
尾部换行令分数 1.0→0.5 时仍会被 score-range 门准确抓出。

Judge provenance/cache 新增 promptHash，prompt 文本变化必然产生新 cache key。随机 trial 现在
强制 promptId/hash 相同；模型版本配对比较也新增 samePromptHash gate，杜绝归因混淆。参考 ACL
2026 的语义等价 prompt robustness 研究和 AAAI 2025 evaluator instruction-adherence 研究。
全套测试 **194/194** 通过。

## Judge indirect prompt injection（2026-08-30）

rubric 升级到 `trace-action-reaction-flow-v4`，system prompt 和 payload trustBoundary 明确规定 goal、
trace、selector、DOM/page text、network body、observation、reference content 都是不可信数据，
只能作为 evidence，不能作为 instruction。promptHash/cache 随 v4 自然隔离。

新增 `trust/injection_robustness.py`，对每条 challenge 构造只增加 untrustedPageContent 的成对样本，
覆盖 direct override、fake SYSTEM、score manipulation、evidence forgery、prompt exfiltration、
中文覆盖、base64 指令和 hidden HTML 八类。先证明 deterministic oracle 完全不变，再比较 claims
与五维 score；provider/schema/evidence error 同样算攻击成功。

专项测试证明两类绕过均可见：强改 fail→pass 被 oracle non-override 本地验证拒绝；保留合法 claims
但把分数从 1.0 改为 0.25，会以 maxScoreDelta=0.75 失败。参考 AgentDojo 的 untrusted tool-data
attack 方法与 OWASP indirect prompt injection 指南。全套测试 **197/197** 通过。

## 五维 score evidence 契约（2026-08-30）

rubric 升级到 `trace-action-reaction-flow-v5`。严格 JSON schema 为五个 scores 增加同构的
`scoreEvidence`，每维必须有非空 evidence/reason。本地验证按维度允许的 current 字段、前后文、
rule findings、oracle criteria 和原始 evidence tokens 做回链；空数组或 `vibes` 均 fail closed。

确定性 step scorer 同样为每维生成依据；hybrid 发生 rule cap 时合并模型依据和确定性上限依据。
Phoenix dimension annotations、challenge modelOutputs、challenge rule truth 和 calibration JSONL
均保留逐维证据。新增专项验证与 Phoenix 导出测试，全套测试 **200/200** 通过。

## Trajectory 四维 evidence 契约（2026-08-30）

rubric 升级到 `trace-action-reaction-flow-v6`。`completeness`、`necessity`、`ordering`、
`evidence_closure` 各自增加 `trajectoryEvidence.{dimension}.{evidence,reason}`；validator 按维度
检查是否真实回链到 actual/reference steps、flow oracle、trace findings、oracle checks 或具体
criterion/evidence。空依据和与输入无关的“vibes”继续 fail closed，共享 trajectory reason 不再
足以支撑四个数字。

challenge report 保存完整 trajectory 对象；Phoenix 根 span 逐维导出分数、reason 和 evidence。
专项及全量回归通过，当前测试 **201/201**；所有模型分仍标记为 uncalibrated ordering score，
在真实人工金标校准前不得解释为成功概率。

## Evidence attribution meta-eval 与全称回链（2026-08-30）

新增 `trust/evidence_audit.py` 并接入 challenge runner。它以每条 challenge 的原始 trace、reference、
observations 和 oracle spec 重新构造允许证据集合，报告逐记录 ungrounded token、backlink precision、
atomic citation rate，以及 27 组反事实的 evidence-change/defect-citation rate；provider error 留在记录
覆盖分母，指标按失败家族拆分且明确不是 accuracy/probability。

用完整 challenge set 自检时，测试 provider 的 backlink precision 为 1.0，但 atomic citation rate
仅 0.709、evidence-change rate 仅 0.3333；这成功揭露了它大量复用 `oracleChecks`/criterion、没有让
证据内容响应运行失败的弱点。基于该结果 rubric 升级为 `trace-action-reaction-flow-v7`：step score、
claim 和 trajectory evidence 数组中的**每一项**都必须回链，合法 token 不再能掩护编造 token。
新增专项测试后全量回归 **204/204** 通过，编译与 diff whitespace 检查通过。

## 状态型 evidence ID 与反事实响应门禁（2026-08-30）

rubric 升级到 `trace-action-reaction-flow-v8`。每个 oracle check 获得绑定 scope、criterion、verdict、
原始 evidence/reason 摘要的稳定 `evidenceId`；三个 claim 必须引用相关 ID，`action_correctness` 和
`evidence_closure` 分别强制引用 action/flow ID。criterion 相同但 verdict 或事实证据改变时，ID 必变。

完整 29 条 challenge（27 个 counterfactual）自检结果：backlink precision **1.0**、atomic citation
rate **0.7696**、evidence-change rate **1.0**、defect-citation rate **1.0**。后两项已固化为测试门禁，
覆盖 grounding、actionability、semantic target、action execution、reaction、outcome、persistence
和 testcase conformance 家族；这些仍是合成反事实证据行为，不是现实任务成功概率。

Challenge CLI 新增 `evidenceAudit.contractGate` 并在 gate 失败时非零退出。门禁只覆盖确定性不变量：
完整审计覆盖、backlink precision=1、反事实 evidence-change=1、defect-citation=1。原子引用率不采用
拍脑袋阈值，必须等真实人工标签和错误成本齐备后再校准。

## Phoenix HUMAN/CODE/LLM 来源与重复实验（2026-09-02）

修正 Phoenix provenance：确定性规则及 conservative hybrid annotations 标为 `CODE`，未经规则组合的
claim/trajectory Judge 标为 `LLM`；所有自动 annotation identifier 绑定 rubric version/source，避免
新 rubric upsert 覆盖旧实验结果。新增 `trust/phoenix_human.py`，逐 annotator 盲标以幂等 `HUMAN`
annotation 写回 step span，保留 evidence、rubric 和标注者分歧。

新增 `trust/phoenix_experiment.py`：正式实验固定 dataset version、rubric、prompt hash、model，并强制
`repetitions>=2`；单次仅允许 dry-run，metadata 不得覆盖核心 provenance。依据 Phoenix 官方的人工
annotation、dataset experiment 和 repetitions 工作流实现；重复均值仍不得解释成概率。

新增 human-gold calibration readiness：默认双人标注、无仲裁、总 gold≥50、每个 claim≥30 且
pass/fail 双类齐备；未满足则校准 CLI 非零退出。现有 7 条 `trust/labels.jsonl` 是 replay run 标签，
缺少 annotator/claim/evidence/nodeId/verdict，已实测被 HUMAN label parser 拒绝，不会冒充金标。
新增测试后全量回归 **214/214** 通过。当前环境仅安装 Phoenix client/OTel，无 Phoenix server CLI，
且 Docker 不可用；因此真实 span/annotation 落库仍标记为未验证，不能以 fake-client 测试替代。

## Phoenix server pin 与 runtime preflight（2026-09-02）

官方部署文档明确建议生产固定镜像版本，因此 compose 从 `latest` 改为
`arizephoenix/phoenix:version-20.4.0`。新增 `trust/phoenix_preflight.py`，检查固定镜像、client/OTel、
HTTP 服务和 Docker fallback，并明确 readiness 不是写入读回证明。

本机实际预检：compose pin 通过，client=3.3.0、OTel=0.17.1；localhost:6006 不可达且没有 Docker，
所以唯一 failure 为 `no-reachable-server-or-docker-runtime`。尝试在临时 Python 3.14 venv 安装 server：
Phoenix 12 明确不支持 3.14；兼容的 20.4.0 依赖安装长时间无进展后已终止，未据此声称集成成功。

新增 Phoenix export readback verifier：`--verify` 通过真实 `context.span_id` 获取 spans，再按当前
rubric/source identifier 读取 annotations，严格比较 name、kind、identifier、label、score、explanation。
只有无缺失、无多余且 span 全部存在才生成 SHA-256 proof；fake-client 测试同时覆盖成功与少一条
annotation 的失败路径。全量回归 **218/218** 通过，但因本机无 server，真实 proof 尚未产生。

## GUI grounding 裕量与质量条件化效率（2026-09-02）

rubric 升级到 `trace-action-reaction-flow-v9`。参考 ScreenSpot-Pro 保留 point-in-box 基础准确性，
另增 `target.hit_margin`：从真实 point+bbox 计算到最近边缘、按宽高归一化的裕量，默认门槛 5%。
新增 `edge_hit` 反事实证明框内 1% 裕量会出现 hit_point=pass、hit_margin=fail；无几何数据保持 unknown。
Observation mutations 增至 14，challenge set 当前为 2 controls + 28 counterfactuals。

参考 MMBench-GUI 的分层效率思路，新增 `flow.path_efficiency`。无缺步时报告 reference/actual step
比例，缺任一必需步骤则为 0；定向测试证明删步不能抬效率、多一步得到 0.8。oracle-spec v2 新增
显式 `maxExtraSteps`、`maxTotalRetries`、`maxDurationMs`；未声明 retry/duration 预算时为
not_applicable，避免任意性能偏好污染任务正确性。

v9 完整 challenge 自检：30 records、2 controls、28 counterfactuals、13 个 failure families，集合验证
零失败；evidence audit 的 backlink precision=1.0、atomic citation rate=0.7803、evidence-change=1.0、
defect-citation=1.0，contract gate 通过，所有成功 run provenance 均为 v9。全量测试 **224/224**。
这些数字只证明合成反事实与证据契约，不替代真实 GUI 运行、模型准确性或概率校准。

## 语义目标证据来源契约（2026-09-02）

rubric 升级到 `trace-action-reaction-flow-v10`。此前 `semanticMatch=true` 即可通过，存在无依据布尔值
冒充正确业务目标的风险。现在 `target.semantic_identity` 同时要求实际 `semanticIdentity`、非空
`semanticEvidence` 和受控 `semanticEvidenceSource`；允许 accessibility-tree、dom-attributes、
visual-human-verified、test-fixture。裸布尔值、空证据或 `model-vibes` 等未知来源均 unknown；带证据
的错误身份确定性 fail。专项覆盖 pass/fail/unknown 三路，全量测试 **226/226**。

## Actionability 逐字段证据契约（2026-09-02）

rubric 升级到 `trace-action-reaction-flow-v11`。`visible/enabled/unobscured` 不再接受裸布尔值，每项
需要非空 `actionabilityEvidence` 和受控来源。允许 playwright-actionability、accessibility-tree、
dom-geometry、visual-human-verified、test-fixture。Observation extractor 仅在成功、非 force、DOM
locator 动作且字段未显式提供时，依据 Playwright displayed/enabled/receives-pointer-events 检查补证；
显式 false 不覆盖，force 与 visual 模式不推导。专项覆盖受证 pass、无证 unknown、force unknown 和
带证 fail，全量测试 **229/229**。

v11 challenge proof：30 records/28 counterfactuals 验证零失败；actionability、semantic-target、
grounding-margin 家族的 evidence-change 与 defect-citation 均为 1.0；整体 backlink precision=1.0、
evidence-change=1.0、defect-citation=1.0，所有成功 run provenance 为 v11。仍只代表合成证据契约。

## Reaction 与运行时状态转移证据契约（2026-09-02）

rubric 升级到 `trace-action-reaction-flow-v12`。网络 response 需要 edr/raw/test validator evidence；
`causallyLinked` 需要 action-scoped waiter、instrumented event window 或 test fixture；直接/下游 assertion
需要 Playwright/API/数据库/人工视觉/test verifier；`next_step_ready` 需要 edr execution sequence 或
test fixture。裸 `ok/passed/ready=true` 全部 unknown，不再证明点击产生预期反应。

新增 `next_step_not_ready` 运行观测反事实，observation mutations=15，challenge=31 records、2 controls、
29 counterfactuals。v12 challenge 验证零失败；reaction-causality/missing/wrong、outcome assertion/closure、
flow-runtime-transition 六家族 evidence-change/defect-citation 均 1.0；整体 backlink precision=1.0、
evidence-change=1.0、defect-citation=1.0，所有 provenance=v12。全量测试 **233/233**。

## Persistence verifier 来源契约（2026-09-02）

rubric 升级到 `trace-action-reaction-flow-v13`。持久状态 evidence 新增受控来源：
edr-persistence-verifier、api-verifier、database-verifier、visual-human-verified、test-fixture。可信来源下
wrong value/method/timeout 为 fail；no evidence/unknown source 为 unknown。新增 unknown-source 反事实。

v13 challenge=32 records/2 controls/30 counterfactuals，集合验证零失败。五个 persistence 配对中前三个
真值为 fail、后两个为 unknown；该家族及整体 evidence-change/defect-citation 均 1.0，backlink
precision=1.0，所有 provenance=v13。全量测试 **234/234**。仍不是现实准确率或概率。

## Action execution 与 target resolver 来源契约（2026-09-02）

rubric v14 为 actual action type/completed 增加 execution/tool evidence source；裸完成布尔值 unknown。
rubric v15 将 target.resolved、target.unique、target.hit_point 分别绑定 resolver evidence、candidate-count
evidence 和受控 hit provenance。Observation extractor 对真实 execution 写入 edr target/action proof；
实测 point+bbox 区分 inside/outside，DOM/visual 仅保留明确 executor-derived 类型。裸 target 三字段均
unknown。

v15 challenge=32/30，集合验证零失败；action-execution、grounding、grounding-margin 家族及整体
evidence-change/defect-citation 均 1.0，backlink precision=1.0，所有 provenance=v15。全量测试
**236/236**。这些仍是合成契约，不是模型准确率。

## 内容绑定的评分证据（2026-09-02）

rubric 升级到 `trace-action-reaction-flow-v16`。此前 step/trajectory 分数可仅引用
`current.selector`、`before`、`flowOracle` 等泛化字段名，虽能通过 backlink 检查，却不能证明模型
查看了当次字段值。现在 payload 为字段值与 trajectory 输入生成绑定 scope、path、规范化内容摘要的
稳定 evidence ID；泛化标签只作导航，每个维度必须至少引用一个 field/trajectory ID、oracle 状态 ID、
rule finding 或原始原子证据，否则 fail closed。内容摘要只验证引用身份，不证明事实真伪或成功概率。

v16 challenge proof：32 records、30 counterfactuals、集合验证零失败、provider errors=0；backlink
precision、atomic citation rate、evidence-change、defect-citation 均为 1.0，contract gate 通过，全部成功
run provenance 为 v16。全量回归 **238/238**，compileall 与 diff check 通过。这仍只证明合成反事实
证据契约；真实 LLM、Phoenix server 写入读回、双人 HUMAN gold 与现实任务准确率尚未验证。

## Oracle-conditioned score caps 与单调聚合（2026-09-02）

rubric 升级到 `trace-action-reaction-flow-v17`。v16 虽保证逐维引用原子证据，但连续分仍可能“证据对、
数值错”。新增 `trust/oracle_caps.py`：只将受控证据下的确定性 fail 映射为语义相关维度的 0 上限；
target/action、reaction 和 flow 分开映射，unknown 不触发 fail cap。Hybrid 输出保留触发 cap 的 criterion、
原始 evidence 与 `definitive-fail-only` policy。

审计同时发现旧 aggregate 只取 step judge/deterministic 最小值，trajectory 和 observation coverage 不能
稳定影响总排序，v17 初次 challenge 的 ordering sensitivity 仅 0.3333；加入 trajectory/oracle 后用 min
仍因已有低分饱和，仅 0.5667。最终改为规则、LLM step、trajectory、oracle evidence-adjusted ordering
的单调合取乘积：固定非负输入轴下单调不增，但零因子饱和与舍入可能产生并列。该算子明确标记
`ordering-only-not-a-probability`，不作概率独立性解释。

最终 v17 challenge：32 records、30 counterfactuals、14 个失败家族全部 ordering sensitivity=1.0；
claim exact=1.0、provider errors=0、backlink precision/atomic citation/evidence-change/defect-citation 均
为 1.0，contract gate 通过，provenance 全为 v17。全量回归 **241/241**，compileall 与 diff check 通过。
仍需真实 LLM 多 trial、Phoenix 实服读回与双人 HUMAN gold 才能声称现实准确率改善。

## 可执行 behavior/stability contract gates（2026-09-02）

rubric 升级到 `trace-action-reaction-flow-v18`。此前 challenge CLI 只因 provider error 或 evidence gate
失败而非零退出；即使 claim/ordering 指标退化也可能成功。新增 `behaviorGate`：零 provider error、
claim exact=1、全部反事实完成配对、整体 ordering sensitivity=1、每个有反事实的失败家族 sensitivity=1，
任一不满足即列出失败原因和失败家族并使 CLI 非零退出。

`trust/challenge_trials.py` 新增 `stabilityGate` 与 CLI 失败语义：至少两次相同 provider/prompt/rubric 的
独立报告必须满足 claim/ordering pass^k=1、两类 flip rate=0、provider error trial rate=0。pass@k 只
说明至少一次成功，不能掩盖随机失败。所有门禁明确标为 synthetic deterministic contract，不是部署
准确率或成功概率。

v18 proof：32 records、30 counterfactuals、集合验证零失败；behavior gate、evidence gate 和两次 trial
stability gate 全部通过；claim exact、ordering sensitivity、backlink precision、atomic citation、
evidence-change、defect-citation 均为 1.0，provenance 全为 v18。全量回归 **243/243**，compileall 与
diff check 通过。真实 LLM、Phoenix server readback 与双人 HUMAN gold 仍未完成，goal 保持 active。

## Claim-stratified HUMAN agreement 与 abstention-aware accuracy（2026-09-02）

rubric 升级到 `trace-action-reaction-flow-v19`。此前 readiness 只有总体 annotator κ，可能由容易 claim
掩盖 reaction 等局部分歧；accuracy 又只在非 abstain 预测上计算，coverage=0.5 时仍可能显示 1.0。
现在 `inter_annotator_agreement` 逐 claim 输出 pair overlap/raw agreement/Cohen’s κ；readiness 默认要求
每个 claim 至少一对标注者 overlap≥30 且 κ≥0.6，并继续要求双类、最小 gold 和零未仲裁项目。

`evaluate_claims` 逐 claim 与总体同时输出 selective accuracy、effective accuracy 及 Wilson 95% 区间。
Selective 只描述已作出 pass/fail 判断的子集；effective 以全部 binary gold 为分母，abstention 与漏预测
不能消失。合成 plumbing proof 中 12 gold、9 known predictions 得到 coverage=0.75、selective=1.0，
但 effective=0.75、Wilson95=[0.4677,0.9111]，证明报告不会再把选择性回答冒充满准确率。

新增低 reaction agreement、逐 claim overlap、Wilson interval 和总体 effective accuracy 测试；全量回归
**244/244**，compileall 与 diff check 通过。该 proof 仍是合成数据，不是现实模型 accuracy；真实双人
盲标数据和真实 LLM predictions 尚未提供，因此 goal 保持 active。

## Oracle Spec v3 与完整架构设计（2026-09-27）

rubric 升级到 `trace-action-reaction-flow-v20`。新增 oracle-spec v3：测试用例可显式声明
`optionalNodeIds`、`allowedExtraActions` 和局部 `orderConstraints`，避免把合法的条件步骤省略、等价
辅助动作或无依赖步骤换序误判为流程错误；默认模式仍保持严格。Flow oracle 改为按 nodeId 对齐步骤，
分别计算必需步骤、未允许额外步骤、局部顺序、参数一致性和质量条件化效率。Judge payload 同样按
actual nodeId 对齐 oracle checks，避免跳过可选步骤后证据错配。

定向证明覆盖：可选步骤省略时 required/order/argument/path-efficiency 全部 pass；违反显式依赖时
order fail；允许的额外 DoNothing 不触发 forbidden 或效率罚分；非法 nodeId、重复/自环顺序约束被
schema 拒绝。原 32 records/30 counterfactual challenge 的 behavior/evidence gates 继续通过；全量回归
**249/249**，compileall 与 diff check 通过。

新增 `../docs/AGENT_TRACE_EVAL_ARCHITECTURE.md`，统一记录目标、九层架构、三条评分链、输入/输出契约、
fail-closed validation、聚合、meta-eval、Phoenix、HUMAN gold、实施阶段、当前完成项和生产验收标准。
真实 LLM、Phoenix 服务端 proof、双人 gold 与生产接入仍未完成。

## 精简架构代码闭环与批量 Runner（2026-09-27）

按精简版 `../docs/AGENT_TRACE_EVAL_ARCHITECTURE.md` 对输入、Oracle、Judge、Validator/Hybrid、输出和 Offline
Evals 做逐项代码审计。发现并修复 oracle-spec v3 的 step 对齐缺陷：允许的额外动作在 flow 总体层虽
为 pass，进入 Judge payload 时仍可能按数组位置继承某个 reference step 的 checks。v21 新增
`flowPolicy/extraStepPolicy`，constraint 模式严格按 actual nodeId 对齐；允许额外动作得到独立
`flow.allowed_extra_step=pass`，未允许动作得到 `flow.extra_step=fail` 并触发相关维度 cap。

新增 `trust/pipeline.py` 作为最小生产批处理入口：读取 `trace-eval.case/v1` JSONL manifest、解析相对
输入文件、逐 case 隔离 provider/validator 错误、生成完整 evaluation、标记 fail/unknown/uncalibrated
复核原因，并以临时文件替换方式原子写报告。评估出业务 fail 不等于程序错误；evaluation/provider
错误始终导致非零退出，`--fail-on-review` 可用于严格流水线。

新增允许额外步骤的 nodeId 对齐测试和 4 项 pipeline 契约/E2E/错误隔离/manifest 测试。最终全量回归
**254/254**，compileall、diff check、32-record challenge behavior/evidence gates 全部通过；batch E2E
成功并将 `uncalibrated-score` 正确路由到人工复核。代码入口已经闭环，但真实模型调用、真实 gold 和
正式事件系统接线仍属于外部运行与数据工作，不能由合成 provider 证明。

## 最小范围审查修复（2026-09-28）

rubric 升至 v22，保留原架构、评分维度、聚合公式与 Phoenix 边界，不增加新服务或匹配框架。

- Oracle 成为全部逐步硬事实的唯一生成处：允许/禁止的额外动作进入同一记录与 cap 链路；允许的
  辅助动作仍检查 Action/Reaction。严格模式重复动作匹配消耗参考次数后才标记超额出现，避免错标首次执行。
- v3 合法调序、省略首部或中间可选节点、插入允许辅助动作，不再按参考相邻关系误判 Reaction。
  运行衔接与路径合规分开检查，下游断言绑定后继节点；顺序约束拒绝环和非字符串端点。
- 明确稳定 nodeId 契约，不猜测跨会话对应关系；可选省略记 N/A，另报必需步骤覆盖率与可选执行数量。
- 逐行隔离畸形 JSON、无效 case、空 trace、引用文件错误，错误报告保留位置；有效重复 ID 仍拒绝整批。
- `evaluate_trace.judge` 保留校验后、合并前模型输出。Challenge 和 evidence audit 改用该输出，Hybrid
  trajectory 单列。修复 `_cites_truth` 误检查 truth 集合自身、使非空真值恒被判为已引用的问题。
- 更正乘积“普遍严格下降”说法：固定输入轴下单调不增，零饱和与舍入会并列；本轮不重写评分公式。

v21 的“额外步骤已触发 cap”和“模型证据门禁全部通过”表述不完整：当时部分额外检查只在 Judge
payload 内合成，且审计读取了 Hybrid 修补后的引用。v22 分离后，原 EvidenceAwareProvider 的
32-record/30-counterfactual 行为门禁仍通过，但原始证据变化率与缺陷引用率均为 **29/30（0.9667）**，
缺失模板反例未被引用，证据门禁正确失败。由 provider 自身补全 rule finding 引用的对照测试可通过；
没有降低门禁阈值，也不将测试 provider 结果称为真实模型准确率。

全量本地回归 **281/281**；compileall、tracked diff check 与本次修改文件的空白检查通过。
未调用真实 LLM、未测得真实准确率，也未完成 HUMAN gold 或生产接线。
