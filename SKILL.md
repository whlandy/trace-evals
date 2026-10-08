---
name: trace-audit
description: >-
  Evaluate whether a recorded automation trace (from edr-cloud-recorder/web-record or
  edr-wd desktop) is worth trusting at all — distinct from "did this one replay pass".
  Answers "can I believe this green", splits trustworthiness into replayability,
  evidential power, and observability, and classifies failures by shape
  (silent-pass / flaky / loud-later / weak) instead of by an invented severity.
  Also deterministically scores MaaFramework replay executions (trust.maa_execution)
  against their golden node tables, keeping the two questions reported separately.
  Use after recording/replaying a trace, before treating its green result as a product
  verdict, and when choosing which traces are fit to feed downstream AI training.
  Do not use as a general test runner or as a network-events recorder.
---

# Trace Audit

Judge the **trustworthiness** of a recorded trace. The recorder's own `evaluate_trace`
answers "did this run go through"; this skill answers "is this trace worth believing",
and it does so per-node with evidence, not with a naked number.

The one-line entry point:

```bash
python3 ../trace-eval/trust/audit.py recordings/<flow>
```

It reads the trace shape from `edr-cloud-recorder/scripts/trace_schema.py` — never from a
copied definition. A copied shape would drift, and a drifted shape would judge traces
against something nothing actually produces.

> ⚠️ Working directory matters: many commands default to a sibling
> `../trace-eval` and `../edr-cloud-recorder`. Set `EDR_RECORDER_HOME` to point at the
> recorder repo when the default layout is not present.

## Input Contract

**trace-eval 只认一种 trace 格式：`edr.success-trace/v2`（v2 节点表，顶层带 `$meta`）。**
`trust/audit.py::load_case_trace` 读 `trace.json`（v2）或 `golden-trace.json`（桌面格式，自动
`desktop_to_v2.convert` 转 v2）。没有第三条读入路径。

**来源不论**：这条 v2 轨迹是语言描述驱动、模板匹配、edr-wd 桌面还是 cloud-recorder web
录的，trace-eval 不区分、也不需要区分。任何来源只要把执行过程落成 v2 节点表，就能被评估——
评估的是"这条轨迹值不值得信"，与来源无关；来源差异编码在 `attach.provenance` / `recognition`
里。因此**不要为某类来源另造输入格式**：统一到 v2 即可兼得"可回放（MaaNodeRunner）+ 可评估"。
（两链路落成 v2 的最小修改方案见 `../maa-fw/docs/design/v2-trace-unified-quality-input.md`。）

## Non-Negotiable Rules

1. **A green replay is not trust.** Replay 100 can coexist with zero assertions and a
   confidence of 50. Never convert "it ran" into "it is correct".
2. **Report evidence, not just a number.** Every finding carries `evidence` and
   `consequence`. A score without evidence is a claim, not a fact.
3. **Scores express ordering only.** Reports carry `uncalibrated-ordering-only` (rule) or
   `uncalibrated-judge-score` (LLM judge). They are not calibrated probabilities. Saying a
   probability would be lying. Do not lower calibration gates after the fact just to go green.
4. **Classify by failure shape, not severity.** `silent-pass`, `flaky`, `loud-later`,
   `weak` are facts; a severity number is a weight disguised as a fact.
5. **A missing runtime observation is `unknown`, never a guess.** Without a recorded
   reaction, the model must return `unknown` rather than fill in a 0.5 or an LLM guess.
6. **Exit is not success.** Check the report body, not just the return code.

## Core Workflow

### 1. Audit a Trace (Default)

```bash
python3 ../trace-eval/trust/audit.py recordings/<flow>
```

Reads `recording.json` + `trace.json`, cross-checks them (compiler-dropped facts), extracts
features, applies rules, and prints a report of findings categorized by failure shape.

### 1.1 Score a Maa Execution (deterministic, no API key)

```bash
python3 -m trust.maa_execution \
  --golden /path/to/maa-trace.json \
  --execution /path/to/maa-execution.json \
  --output /path/to/maa-evaluation.json
```

Use this after MaaNodeRunner produced an `edr.maa-execution-trace/v1` artifact. It validates
the golden digest, node set and order, then reports completion rate, action accuracy,
trajectory order, retry efficiency and visual-match confidence. It rejects incomplete golden
traces, unsupported execution schemas, duplicate or missing node IDs, and golden digest
mismatches; report `taskSuccess` first, followed by failed node IDs and component metrics.
An optional node is satisfied by a skip only when the golden trace marks it optional.

Keep this separate from section 1: it answers "did this one replay follow the golden path",
not "is the golden trace trustworthy". A high execution score is never evidence that the
golden trace is worth believing — report both conclusions, never merge them.

### 2. Deepen with Per-Step Scoring

```bash
python3 -m trust.step_score <trace-dir>
python3 -m trust.trajectory_match <actual-trace> <reference-trace> strict
```

Per-step rules output five dimensions: action correctness, argument quality, context fit,
evidence quality, replay safety. The trajectory matcher compares an actual execution
against a golden path.

### 3. Add a Semantic Judge (optional, needs an API key)

```bash
python3 -m trust.evaluate <trace-dir> --goal "策略保存且副作用得到验证" --model <model>
python3 -m trust.evaluate <actual> --reference <ref> --goal "..." --model <model>
python3 -m trust.judge_validate <trace-dir> --goal "..." --model <model>
```

The judge uses the provider's strict JSON-Schema output. Rule-confirmed hard defects cap the
model score; the model can never override them. Without an API key the local rule scoring and
trajectory matching above still work.

### 4. Inject Defects & Meta-Evaluate (negative control)

```bash
python3 -m trust.mutate <trace-dir>           # 10 defect injectors produce adversarial traces
python3 -m trust.validate                      # meta-eval: does this evaluator actually catch them?
```

Mutated traces must score strictly lower than the original (monotonicity). `validate` is the
meta-evaluation that keeps the evaluator honest.

### 5. Prove Stability with Repeated Replay (truth labels)

```bash
python3 -m trust.replay_lab <trace-dir>       # repeat-replay, emits observations per run
```

### 6. Visualize & Run Phoenix Experiments (optional)

```bash
docker compose -f ../trace-eval/docker-compose.phoenix.yml up -d
python3 -m trust.phoenix_export <trace-dir> --dry-run     # check first, don't send
python3 -m trust.phoenix_export <trace-dir> [--evaluation evaluation.json]
python3 -m trust.phoenix_preflight                          # before startup or CI
```

Phoenix is only the display/annotation/experiment platform; the scoring criteria stay versioned
in this repo. Annotations distinguish source: rules/hybrid = `CODE`, judge = `LLM`, double-blind
human = `HUMAN`. Formal experiments must pin dataset/rubric/prompt/model versions and use
`repetitions>=2`; single runs are dry-run only. Server image is pinned to
`arizephoenix/phoenix:version-20.4.0` — never `latest`.

### 7. Calibrate Only with Human Gold Labels

```bash
python3 -m trust.calibrate human-labels.jsonl
```

The default readiness gate requires ≥2 annotators, no pending arbitrations, ≥50 total gold,
≥30 per claim over both pass/fail. Not meeting it exits non-zero. The gate parameters may be
adjusted and saved with the report, but not lowered after the fact to force a green.

## Failure Shape Cheat-Sheet

| Shape | Meaning | Why it hurts | Suspect |
|---|---|---|---|
| `silent-pass` | wrong yet reported success | false confidence, worse than failure | zero/tautological assertions, missing side-effect check |
| `flaky` | outcome flips with start state | unrepeatable, untrustworthy | blind toggle clicks, `nth-of-type` absolute paths, volatile anchors |
| `loud-later` | green now, red later | signals arrive too late | conditional overlay treated as mandatory, state mutated by prior run |
| `weak` | never fails but proves nothing | green is empty | existence-only assertions, writes without `expectedBody` |

## Reference Router

| Reference | Read When |
|---|---|
| [AGENT_EVAL_RUBRIC.md](docs/AGENT_EVAL_RUBRIC.md) | judging per-step/whole-run with a model rubric (action/reaction/conformance) |
| [TRACE_STEP_EVAL_DESIGN.md](docs/TRACE_STEP_EVAL_DESIGN.md) | designing or changing per-step evaluation |
| [PLAN.md](docs/PLAN.md) | the three-axis philosophy (replayability/evidence/observability) and success criteria |
| [EVAL_PLATFORM_MIGRATION_DESIGN.md](docs/EVAL_PLATFORM_MIGRATION_DESIGN.md) | next-phase migration: golden replay, business oracle, experiment platform |
| `../edr-cloud-recorder/scripts/trace_schema.py` | the authoritative trace shape |
| `../edr-cloud-recorder/scripts/maa_contract.py` | the node-contract checks maa-fw accepts |

## Repository Map

```text
SKILL.md        This skill
README.md       Longer prose + all CLI examples
docs/PLAN.md         Design rationale, three axes, falsifiable success criteria
docs/AGENT_EVAL_RUBRIC.md  Model-judge rubric (action / reaction / test-case conformance)
docs/TRACE_STEP_EVAL_DESIGN.md  Per-step evaluation design
trust/audit.py      One-command audit report (the default entry)
trust/features.py   Extract facts, no scoring
trust/rules.py      Facts -> evidenced findings
trust/crosscheck.py recording.json <-> trace.json, finds compiler-dropped facts
trust/score.py      Penalty rollup (ordering only)
trust/maa_execution.py  Deterministic scoring of one Maa execution vs its golden trace
trust/step_score.py Per-step five-dimension scoring
trust/trajectory_match.py  Actual vs golden path comparison
trust/mutate.py     10 defect injectors (negative control)
trust/replay_lab.py Repeated replay for truth labels + observations
trust/validate.py   Meta-eval: evaluate the evaluator
trust/judge.py / evaluate.py / hybrid.py  Optional LLM judge + hybrid scoring
trust/calibrate.py  Human-gold calibration + readiness gate
trust/phoenix_*.py  Phoenix visualization / experiments / human labels
test/               Evaluator, injection, and shape checks
```

## Self-Check

After changing rules, scoring, or the trace shape contract:

```bash
pytest -q
python3 -m trust.validate            # mutations still caught
python3 -m trust.phoenix_preflight   # if you touch Phoenix wiring
```

Do not claim an evaluator works from an example output alone. The failure mode being guarded
must be exercised, and any new rule needs both a trace it flags and one it lets through.
