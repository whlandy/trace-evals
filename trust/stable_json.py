#!/usr/bin/env python3
"""字符级稳定的 JSON 序列化。

同一输入在任何进程、任何字典插入顺序下都产生逐字节相同的输出。
用于：

- Golden Result 的落盘与比对（Round 0 契约基线）；
- 摘要计算（与 maa_execution.trace_digest 的规范化形式保持一致）；
- 后续 Experiment Artifact 的 digest 寻址（迁移设计 5.3）。

稳定靠三件事：键排序、固定分隔符、固定 unicode 处理。不改语义 ——
输出仍是合法 JSON，round-trip 不丢字段。
"""

from __future__ import annotations

import json


def dumps_stable(value: object) -> str:
    """排序键 + 紧凑分隔符 + 保留 unicode，逐字节确定。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads_stable(text: str) -> object:
    """与 dumps_stable 配对的解析；规范化校验由调用方做。"""
    return json.loads(text)


def stable_roundtrip(value: object) -> object:
    """dumps→loads 往返，用于验证公共结构 JSON 可序列化且无信息丢失。"""
    return json.loads(dumps_stable(value))
