# maa-trace-eval

评估 [edr-cloud-recorder](../edr-cloud-recorder) 产出的 maa 轨迹**值不值得信**。

```bash
python3 trust/audit.py <轨迹目录>
```

## 它回答的是另一个问题

录制器自带的 `evaluate_trace` 评的是「**这一次回放**跑得怎么样」。
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
```

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

进度和每一轮的结论见 [trust/STATUS.md](trust/STATUS.md)，方案见 [PLAN.md](PLAN.md)。
