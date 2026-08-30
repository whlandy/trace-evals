"""定位 edr-cloud-recorder。

这个项目评的是**它**产出的轨迹，而轨迹的形状定义只在它那边（`trace_schema.py`）。
所以这里不复制一份形状定义，而是把它的 `scripts/` 和 `assets/` 挂进 sys.path ——
复制出来的那份一定会漂移，漂移之后这个评估器就会拿自己那份形状去评轨迹，
正好放过「产出的形状读不懂」这一整类问题。

路径可以用 EDR_RECORDER_HOME 覆盖，默认是同一个 ai-projects 下的兄弟目录。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULT_HOME = Path(__file__).resolve().parent.parent.parent / "edr-cloud-recorder"
MARKER = Path("scripts") / "trace_schema.py"


def recorder_home() -> Path:
    home = Path(os.environ.get("EDR_RECORDER_HOME") or DEFAULT_HOME).expanduser()
    if not (home / MARKER).exists():
        raise RuntimeError(
            f"找不到 edr-cloud-recorder（在 {home} 下没看到 {MARKER}）。\n"
            f"用 EDR_RECORDER_HOME 指定它的位置。")
    return home


def install_recorder_path(*, with_tests: bool = False) -> Path:
    """把录制器的模块目录挂进 sys.path，返回它的根目录。"""
    home = recorder_home()
    parts = ["scripts", "assets"] + (["test"] if with_tests else [])
    for part in parts:
        path = str(home / part)
        if path not in sys.path:
            sys.path.insert(0, path)
    return home
