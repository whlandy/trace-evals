"""测试要能同时看到本项目、录制器的模块，以及录制器 test/ 下的 trace_v1。

trace_v1 是**录制器**的测试辅助（平铺形状 → v2 的翻译器）。这里直接引用它，
不复制：复制一份就等于在这个项目里再写一份形状定义，而它一定会漂移。
引用的代价是录制器改名/挪走时这里会红 —— 红得明明白白，比悄悄用旧形状强。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test"))

from trust._recorder import install_recorder_path   # noqa: E402

install_recorder_path(with_tests=True)
