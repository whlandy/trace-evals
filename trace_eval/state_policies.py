#!/usr/bin/env python3
"""Round 5：状态策略接口 —— reset / carry-forward / snapshot-restore。

设计原则（§5.5 副作用）：写操作默认串行、隔离需要显式声明。离线回放
世界里，「目标系统状态」= Experiment 目录内的状态存储
``state-store.json``；策略决定**每个 trial 看到什么起点**、
**trial 结束后状态如何流转**。

（真实目标系统接入后，写操作由 Adapter 的 run() 执行；本模块的职责不变：
管理状态摘要、trial 间流转与隔离 —— 只是「状态」从模拟存储变成真实系统。）

三种策略：

- ``reset``            每个 trial 前把状态存储重置回初始状态（默认，最安全）；
- ``carry-forward``    下一 trial 复用上一 trial 结束时的状态 ——
                       用于**暴露**状态依赖缺陷（上一遍的写污染了这一遍）；
- ``snapshot-restore`` trial 开始打快照、结束恢复 —— 状态不回灌，
                       但恢复动作本身被记录（清理失败的可见点）。

Case 声明：``case.environment.statePolicy``：

    {"policy": "carry-forward"}            显式命名
    {"reset": "per-trial"}                 简写 → reset
    （缺省 → reset）
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from trust.stable_json import dumps_stable

STATE_STORE_NAME = "state-store.json"
STAGE_NAME = "artifacts/state-store.json"
_SNAPSHOT_NAME = ".state-snapshot.json"


def initial_state() -> dict:
    """Experiment 初始状态：空（干净起点，digest 可复现）。"""
    return {}


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(dumps_stable(state) + "\n", encoding="utf-8")

    def digest(self) -> str:
        """状态摘要（初始状态摘要 = 每 trial 记录的可复现指纹）。"""
        import hashlib
        canonical = dumps_stable(self.load()).encode("utf-8")
        return "sha256:" + hashlib.sha256(canonical).hexdigest()

    def write_keys_applied(self, keys: list[str], trial: int) -> None:
        """模拟写副作用落地（真实环境里这由 Adapter 的 run() 完成）。"""
        if not keys:
            return
        state = self.load()
        writes = state.setdefault("writes", {})
        for key in keys:
            writes[key] = trial
        self.save(state)

    def restore(self, from_file: Path) -> None:
        shutil.copy2(from_file, self.path)


def write_keys_of(case) -> list[str]:
    env = case.environment or {}
    return [str(effect).split(":", 1)[1]
            for effect in (env.get("sideEffects") or [])
            if str(effect).split(":", 1)[0] == "write"]


def policy_name_of(case) -> str:
    """从 case.environment.statePolicy 解析策略名（缺省 reset）。"""
    policy = (case.environment or {}).get("statePolicy") or {}
    name = policy.get("policy") or ("reset" if policy else None)
    if name in ("reset", "carry-forward", "snapshot-restore"):
        return name
    return "reset"


@dataclass
class StateRecord:
    """一个 trial 的状态账目：初始摘要 + 结束后流转结果。"""
    policy: str
    trial: int
    initial_digest: str
    initial_state: dict
    after: dict = field(default_factory=dict)


class _BasePolicy:
    name = "base"

    def __init__(self, experiment_dir: Path):
        self.store = StateStore(Path(experiment_dir) / STATE_STORE_NAME)

    def _stage(self, run_dir: Path) -> None:
        """把当前状态暂存进 run workspace（Oracle 可读 run 目录内证据）。"""
        target = Path(run_dir) / STAGE_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.store.path, target) \
            if self.store.path.exists() else target.write_text(
                dumps_stable({}) + "\n", encoding="utf-8")

    def prepare(self, case, trial: int, run_dir: Path) -> StateRecord:
        raise NotImplementedError

    def after_trial(self, case, trial: int, success: bool,
                    run_dir: Path) -> StateRecord:
        raise NotImplementedError


class ResetPolicy(_BasePolicy):
    name = "reset"

    def prepare(self, case, trial: int, run_dir: Path) -> StateRecord:
        self.store.save(initial_state())  # 重置回初始（干净起点）
        record = StateRecord(self.name, trial, self.store.digest(),
                             self.store.load())
        self._stage(run_dir)
        return record

    def after_trial(self, case, trial: int, success: bool,
                    run_dir: Path) -> StateRecord:
        # 写副作用记录到状态账目，但下一次 prepare 会重置 —— 隔离生效
        record = StateRecord(self.name, trial, self.store.digest(),
                             self.store.load())
        record.after = {"applied": write_keys_of(case) if success else [],
                        "isolated": True}
        return record


class CarryForwardPolicy(_BasePolicy):
    name = "carry-forward"

    def prepare(self, case, trial: int, run_dir: Path) -> StateRecord:
        record = StateRecord(self.name, trial, self.store.digest(),
                            self.store.load())
        self._stage(run_dir)
        return record

    def after_trial(self, case, trial: int, success: bool,
                    run_dir: Path) -> StateRecord:
        record = StateRecord(self.name, trial, self.store.digest(),
                             self.store.load())
        if success:
            self.store.write_keys_applied(write_keys_of(case), trial)
        record.after = {"applied": write_keys_of(case) if success else [],
                        "isolated": False}
        return record


class SnapshotRestorePolicy(_BasePolicy):
    name = "snapshot-restore"

    def prepare(self, case, trial: int, run_dir: Path) -> StateRecord:
        snapshot = Path(run_dir) / _SNAPSHOT_NAME
        if self.store.path.exists():
            shutil.copy2(self.store.path, snapshot)
        else:
            snapshot.write_text(dumps_stable({}) + "\n", encoding="utf-8")
        record = StateRecord(self.name, trial, self.store.digest(),
                             self.store.load())
        self._stage(run_dir)
        return record

    def after_trial(self, case, trial: int, success: bool,
                    run_dir: Path) -> StateRecord:
        snapshot = Path(run_dir) / _SNAPSHOT_NAME
        record = StateRecord(self.name, trial, self.store.digest(),
                             self.store.load())
        if success:
            self.store.write_keys_applied(write_keys_of(case), trial)
        if snapshot.exists():
            self.store.restore(snapshot)  # 状态不回灌
        record.after = {"applied": write_keys_of(case) if success else [],
                        "restored": True}
        return record


_POLICIES = {
    "reset": ResetPolicy,
    "carry-forward": CarryForwardPolicy,
    "snapshot-restore": SnapshotRestorePolicy,
}


def policy_for(case, experiment_dir: Path):
    return _POLICIES[policy_name_of(case)](experiment_dir)


def known_policies() -> list[str]:
    return sorted(_POLICIES)
