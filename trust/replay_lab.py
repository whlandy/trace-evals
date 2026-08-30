#!/usr/bin/env python3
"""重复回放取标签 —— 可信度模型唯一的真值来源。

**为什么必须跑两遍以上**：这些轨迹里普遍带写请求，第一遍会改动第二遍的起点。
所以「第二遍」不是重复实验，是换了初始条件的新实验 —— maa-flow5 就是这么
在 9/17 和 15/17 之间振荡的。跑一遍看不出这件事，而看不出就等于没有标签。

标签四类：

    stable-green  每一遍都成功，且分数一致
    drifting      每一遍都成功，但分数不同（同样的操作，结论不一样）
    flip          有的遍成功有的遍失败 —— 最有价值的一类
    stable-red    每一遍都失败

外加一类不是标签的结果：

    invalid       会话在跑的过程中失效了。这时候每条轨迹都会「失败」，
                  但失败的原因和轨迹本身无关 —— 把它当红标签就是在伪造真值。

    python3 trust/replay_lab.py --k 2 <轨迹目录> ...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trust._recorder import install_recorder_path              # noqa: E402
RECORDER = install_recorder_path()

from playwright.sync_api import sync_playwright                # noqa: E402
from auth_setup import export_state, login                     # noqa: E402
from chrome_path import resolve_chrome                         # noqa: E402
from rec_config import load_config, with_defaults              # noqa: E402
from replay_trace import evaluate_trace, load_trace, replay_trace  # noqa: E402

# 会话失效的痕迹。replay_trace 会在第一步就把它认出来（_assert_landed），
# 我们据此把这一遍标成 invalid 而不是红 —— 否则每条轨迹都会被冤枉。
AUTH_MARKS = ("登录态", "认证页", "会话")


def _run_once(page, case: Path, timeout_ms: int) -> dict:
    golden = load_trace(case / "trace.json")
    started = time.time()
    execution = replay_trace(page, golden, template_root=case,
                             targeting="dom_first", timeout_ms=timeout_ms)
    report = evaluate_trace(golden, execution)
    failed = next((s for s in execution["steps"] if s.get("status") == "failed"), None)
    error = str(failed.get("error")) if failed else ""
    return {
        "taskSuccess": report["taskSuccess"],
        "score": report["score"],
        "stepCompletionRate": report["stepCompletionRate"],
        "failedNode": failed.get("nodeId") if failed else None,
        "error": error[:160],
        "invalid": any(mark in error for mark in AUTH_MARKS),
        "durationS": round(time.time() - started, 1),
    }


def label_of(runs: list[dict]) -> str:
    if any(r["invalid"] for r in runs):
        return "invalid"
    successes = {r["taskSuccess"] for r in runs}
    if successes == {True}:
        return "stable-green" if len({r["score"] for r in runs}) == 1 else "drifting"
    if successes == {False}:
        return "stable-red"
    return "flip"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", nargs="+")
    parser.add_argument("--k", type=int, default=2, help="每条轨迹跑几遍")
    parser.add_argument("--timeout-ms", type=int, default=20_000)
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "labels.jsonl"))
    args = parser.parse_args(argv)

    cfg = load_config()
    base = cfg["baseUrl"]
    start = with_defaults(cfg)["url"]
    auth_dir = Path(os.environ.get("REC_STATE_DIR", Path.cwd() / ".auth"))
    auth_dir.mkdir(parents=True, exist_ok=True)

    out = Path(args.out)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False, executable_path=resolve_chrome(),
            args=["--ignore-certificate-errors"])
        # 先登一次并导出登录态。会话中途失效会把每条轨迹都变成红，
        # 那种红和轨迹本身无关 —— 是在伪造真值。
        ctx = browser.new_context(ignore_https_errors=True, no_viewport=True,
                                  base_url=base)
        page = ctx.new_page()
        page.goto(start, wait_until="domcontentloaded")
        login(page)
        export_state(ctx, page, auth_dir)
        ctx.close()

        for case_arg in args.cases:
            case = Path(case_arg)
            runs = []
            for i in range(args.k):
                ctx = browser.new_context(
                    ignore_https_errors=True, no_viewport=True, base_url=base,
                    storage_state=str(auth_dir / "state.json"))
                page = ctx.new_page()
                try:
                    run = _run_once(page, case, args.timeout_ms)
                except Exception as error:                   # 回放器自己抛的
                    run = {"taskSuccess": False, "score": 0.0,
                           "stepCompletionRate": 0.0, "failedNode": None,
                           "error": f"{type(error).__name__}: {error}"[:160],
                           "invalid": any(m in str(error) for m in AUTH_MARKS),
                           "durationS": None}
                finally:
                    ctx.close()
                runs.append(run)
                print(f"  {case.name} 第 {i + 1} 遍: "
                      f"success={run['taskSuccess']} score={run['score']} "
                      f"{run['failedNode'] or ''} {run['error'][:70]}")

            record = {"case": case.name, "k": args.k, "label": label_of(runs),
                      "runs": runs, "at": datetime.now().isoformat(timespec="seconds")}
            with out.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"{case.name} → {record['label']}\n")
        browser.close()
    print(f"标签追加到 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
