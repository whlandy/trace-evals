---
name: trace-evals
description: >-
  Audit whether EDR golden traces are trustworthy and deterministically score MaaFramework replay
  executions against their golden node tables. Use for trace quality, replay reliability, evidence
  strength, or post-replay Maa evaluation; do not use to record, repair, or replay traces.
---

# Trace Evals

Keep two questions separate:

1. **Can the golden trace be trusted?** Audit its replayability, evidential power, and observation
   coverage before treating it as a reference.
2. **Did this Maa replay match its golden trace?** Score one completed execution deterministically.

When both artifacts are available, run both checks and report both conclusions. Never present a
successful replay or a high execution score as proof that the golden trace is trustworthy.

## Audit a golden trace

Use this for an EDR recording directory containing `trace.json` or `golden-trace.json`:

```bash
python3 trust/audit.py /path/to/recording-directory
```

Report evidence-backed findings by axis. Treat the trust score as ordering only, not a calibrated
probability. Say when `recording.json` is missing because that disables recording-to-trace checks.

## Evaluate a Maa execution

Use this after MaaNodeRunner produced an `edr.maa-execution-trace/v1` artifact:

```bash
python3 -m trust.maa_execution \
  --golden /path/to/maa-trace.json \
  --execution /path/to/maa-execution.json \
  --output /path/to/maa-evaluation.json
```

Reject incomplete golden traces, unsupported execution schemas, duplicate or missing node IDs,
and golden digest mismatches. Report `taskSuccess` first, followed by failed node IDs and the
component metrics. An optional node is satisfied by a skip only when the golden trace marks it
optional.

This evaluation proves only that encoded Maa recognitions, actions, and verifiers followed the
expected path. It does not prove an unencoded business outcome or visual quality.
