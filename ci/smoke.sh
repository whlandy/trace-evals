#!/usr/bin/env bash
# trace-evals/v2 CI smoke —— 固定 Dataset + Gate 的重复、可解释发布判断。
#
# 用法：bash ci/smoke.sh
# 稳定退出码：
#   0 = Gate 放行（固定 Dataset 上无新增失败 / inconclusive / 漂移）
#   1 = Gate 阻断（新增失败、inconclusive、漂移 —— 见 gate.report.md 的规则与证据）
#   2 = 配置错误 / Artifact 缺失 / 基础设施错误（判定本身不可信）
set -uo pipefail
cd "$(dirname "$0")/.."

export PYTHONPATH="$(pwd):${PYTHONPATH:-}"
SNAPSHOT=test/fixtures/contracts/dataset
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 1) Dataset 验证（配置错误 → 2）
if ! python3 -m trace_eval.datasets validate "$SNAPSHOT" >/dev/null; then
  echo "smoke: Dataset 验证失败（配置错误）"
  exit 2
fi

# 2) 回放：同一固定 Dataset 的两个 Experiment（baseline / candidate）
if ! python3 -m trace_eval.runner run "$SNAPSHOT" \
    --experiment-id ci-baseline --store "$WORK/store" --trials 2 >/dev/null; then
  echo "smoke: baseline 回放失败（配置或基础设施错误）"
  exit 2
fi
if ! python3 -m trace_eval.runner run "$SNAPSHOT" \
    --experiment-id ci-candidate --store "$WORK/store" --trials 2 >/dev/null; then
  echo "smoke: candidate 回放失败（配置或基础设施错误）"
  exit 2
fi

# 3) Gate 判定：新增失败 / inconclusive / 漂移 → 1；配置或基础设施错误 → 2
python3 -m trace_eval.gates check \
  "$WORK/store/ci-candidate" \
  --baseline "$WORK/store/ci-baseline" \
  --out "$WORK/gate"
exit $?
