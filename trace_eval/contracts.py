#!/usr/bin/env python3
"""Round 1：统一 Contract 与 Result 协议（trace-evals/v2）。

定义迁移设计文档 §7 的公共数据结构与严格解析：

- EvalCase         trace-evals.case/v1     Dataset 的 Case 声明（输入/执行器/Oracle/环境）
- ArtifactRef      (path, digest)          摘要寻址的 Artifact 引用（设计 §5.3）
- EvalRun          trace-evals.run/v1      一次回放执行（四态 status、Artifact 摘要）
- EvaluatorResult  trace-evals.result/v1   单个 evaluator 的结构化结论

解析规则（设计 §7 与 Round 1 验收标准）：

- 未知**主版本**拒绝（ContractError），错误信息能区分「同族换主版本」与「族不对」；
- 未知附加字段**保留**（进 extra，不丢弃也不报错）——破坏兼容的是删字段，不是加字段；
- 已知字段严格校验（类型、枚举、fail 结果的必备证据字段）；
- JSON round-trip 不丢字段：to_dict ↔ from_dict 往返后逐字段相等。

公共结构的稳定序列化复用 trust.stable_json（字符级稳定，与 digest 规范化一致）。
"""

from __future__ import annotations

import dataclasses
from typing import Any, Literal, Optional

from trust.stable_json import stable_roundtrip

SCHEMA_CASE = "trace-evals.case/v1"
SCHEMA_RUN = "trace-evals.run/v1"
SCHEMA_RESULT = "trace-evals.result/v1"

RUN_STATUSES = ("completed", "invalid", "infra_error", "cancelled")
VERDICTS = ("pass", "fail", "inconclusive")
SCOPES = ("case", "run", "experiment")
CALIBRATIONS = ("deterministic", "ordering-only", "calibrated", "judge-unvalidated")


class ContractError(ValueError):
    """公共结构版本或字段违例 —— 迁移协议层的第一道闸。"""


def _check_schema(value: Any, expected: str, what: str) -> None:
    if not isinstance(value, dict):
        raise ContractError(f"{what} 必须是 JSON 对象，实际是 {type(value).__name__}")
    schema = value.get("schema")
    if schema == expected:
        return
    family = expected.rsplit("/", 1)[0]
    if isinstance(schema, str) and schema.rsplit("/", 1)[0] == family:
        raise ContractError(f"{what}: 未知主版本 {schema!r}（期望 {expected!r}）")
    raise ContractError(f"{what}: 缺少或无效 schema {schema!r}（期望 {expected!r}）")


def _nonempty_str(value: Any, field: str, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{what}.{field} 必须是非空字符串")
    return value


def _dict(value: Any, field: str, what: str) -> dict:
    if not isinstance(value, dict):
        raise ContractError(f"{what}.{field} 必须是对象")
    return value


def _str_list(value: Any, field: str, what: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ContractError(f"{what}.{field} 必须是字符串数组")
    return list(value)


def _num(value: Any, field: str, what: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{what}.{field} 必须是数字")
    return value


def _extract(value: dict, mapping: dict[str, str], what: str,
             required: set[str], default: Any) -> dict[str, Any]:
    """按 camelCase→snake_case 映射取值；required 缺失即拒绝；unknown 键进 extra。"""
    kwargs: dict[str, Any] = {}
    for json_name, attr in mapping.items():
        if json_name in value:
            kwargs[attr] = value[json_name]
        elif json_name in required:
            raise ContractError(f"{what}.{json_name} 缺失（必填字段）")
        else:
            kwargs[attr] = default
    extra = {key: item for key, item in value.items()
             if key != "schema" and key not in mapping}
    kwargs["extra"] = extra if extra else None
    return kwargs


# ── EvalCase ───────────────────────────────────────────────────────

CASE_FIELDS = {
    "id": "id", "input": "input", "executor": "executor",
    "expected": "expected", "oracles": "oracles", "environment": "environment",
    "tags": "tags", "metadata": "metadata",
}


@dataclasses.dataclass
class EvalCase:
    id: str
    executor: str
    input: dict
    expected: Optional[dict] = None
    oracles: Optional[list] = None
    environment: Optional[dict] = None
    tags: Optional[list] = None
    metadata: Optional[dict] = None
    extra: Optional[dict] = None

    def to_dict(self) -> dict:
        out = {"schema": SCHEMA_CASE, "id": self.id, "executor": self.executor,
               "input": self.input}
        for json_name, attr in CASE_FIELDS.items():
            if json_name in ("id", "executor", "input"):
                continue
            value = getattr(self, attr)
            if value is not None:
                out[json_name] = value
        if self.extra:
            out.update(self.extra)
        return out

    @classmethod
    def from_dict(cls, value: dict, *, require_schema: bool = True) -> "EvalCase":
        # 嵌套在版本化 Dataset 里的 Case 继承 Dataset 的主版本，可省略自身 schema；
        # 但只要写了 schema，就必须精确匹配（未知主版本照样拒绝）。
        if "schema" in value:
            _check_schema(value, SCHEMA_CASE, "EvalCase")
        elif require_schema:
            raise ContractError(f"EvalCase: 缺少 schema（期望 {SCHEMA_CASE!r}）")
        kwargs = _extract(value, CASE_FIELDS, "EvalCase",
                          required={"id", "input", "executor"}, default=None)
        case = cls(
            id=_nonempty_str(kwargs["id"], "id", "EvalCase"),
            executor=_nonempty_str(kwargs["executor"], "executor", "EvalCase"),
            input=_dict(kwargs["input"], "input", "EvalCase"),
        )
        for attr, json_name in (( "expected", "expected"), ("oracles", "oracles"),
                                ("environment", "environment"), ("tags", "tags"),
                                ("metadata", "metadata")):
            raw = kwargs[attr]
            if raw is None:
                continue
            if attr in ("oracles", "tags") and not isinstance(raw, list):
                raise ContractError(f"EvalCase.{json_name} 必须是数组")
            if attr in ("expected", "environment", "metadata") and not isinstance(raw, dict):
                raise ContractError(f"EvalCase.{json_name} 必须是对象")
            setattr(case, attr, raw)
        case.extra = kwargs["extra"]
        return case


# ── ArtifactRef ────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class ArtifactRef:
    path: str
    digest: Optional[str] = None

    def to_dict(self) -> dict:
        out = {"path": self.path}
        if self.digest is not None:
            out["digest"] = self.digest
        return out

    @classmethod
    def from_dict(cls, value: dict) -> "ArtifactRef":
        if not isinstance(value, dict):
            raise ContractError("ArtifactRef 必须是对象")
        return cls(path=_nonempty_str(value.get("path"), "path", "ArtifactRef"),
                   digest=value.get("digest"))


# ── EvalRun ────────────────────────────────────────────────────────

RUN_FIELDS = {
    "runId": "run_id", "experimentId": "experiment_id", "caseId": "case_id",
    "variantId": "variant_id", "trial": "trial", "status": "status",
    "startedAt": "started_at", "finishedAt": "finished_at",
    "artifacts": "artifacts", "environment": "environment",
}


@dataclasses.dataclass
class EvalRun:
    run_id: str
    experiment_id: str
    case_id: str
    variant_id: str
    trial: int
    status: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    artifacts: Optional[dict] = None
    environment: Optional[dict] = None
    extra: Optional[dict] = None

    def to_dict(self) -> dict:
        out = {json_name: getattr(self, attr)
               for json_name, attr in RUN_FIELDS.items()}
        if self.extra:
            out.update(self.extra)
        out["schema"] = SCHEMA_RUN
        return out

    @classmethod
    def from_dict(cls, value: dict) -> "EvalRun":
        _check_schema(value, SCHEMA_RUN, "EvalRun")
        kwargs = _extract(value, RUN_FIELDS, "EvalRun",
                          required={"runId", "experimentId", "caseId", "variantId",
                                   "trial", "status"}, default=None)
        trial = kwargs["trial"]
        if type(trial) is not int or trial < 0:
            raise ContractError(f"EvalRun.trial 必须是非负整数，实际 {trial!r}")
        status = kwargs["status"]
        if status not in RUN_STATUSES:
            raise ContractError(
                f"EvalRun.status 必须是 {RUN_STATUSES} 之一，实际 {status!r}")
        run = cls(
            run_id=_nonempty_str(kwargs["run_id"], "runId", "EvalRun"),
            experiment_id=_nonempty_str(kwargs["experiment_id"], "experimentId", "EvalRun"),
            case_id=_nonempty_str(kwargs["case_id"], "caseId", "EvalRun"),
            variant_id=_nonempty_str(kwargs["variant_id"], "variantId", "EvalRun"),
            trial=trial, status=status,
            started_at=kwargs["started_at"], finished_at=kwargs["finished_at"],
        )
        if kwargs["artifacts"] is not None:
            artifacts = _dict(kwargs["artifacts"], "artifacts", "EvalRun")
            for name, ref in artifacts.items():
                ArtifactRef.from_dict(ref)  # 逐条严格解析
            run.artifacts = artifacts
        if kwargs["environment"] is not None:
            run.environment = _dict(kwargs["environment"], "environment", "EvalRun")
        run.extra = kwargs["extra"]
        return run


# ── EvaluatorResult ────────────────────────────────────────────────

RESULT_FIELDS = {
    "key": "key", "scope": "scope", "verdict": "verdict", "score": "score",
    "calibration": "calibration", "failureCode": "failure_code",
    "nodeId": "node_id", "expected": "expected", "observed": "observed",
    "evidence": "evidence", "comment": "comment", "evaluator": "evaluator",
}


@dataclasses.dataclass
class EvaluatorResult:
    key: str
    scope: str
    verdict: str
    evaluator: dict
    score: Optional[float] = None
    calibration: Optional[str] = None
    failure_code: Optional[str] = None
    node_id: Optional[str] = None
    expected: Any = None
    observed: Any = None
    evidence: Optional[list] = None
    comment: Optional[str] = None
    extra: Optional[dict] = None

    def to_dict(self) -> dict:
        out = {"schema": SCHEMA_RESULT}
        for json_name, attr in RESULT_FIELDS.items():
            out[json_name] = getattr(self, attr)
        if self.extra:
            out.update(self.extra)
        return out

    @classmethod
    def from_dict(cls, value: dict) -> "EvaluatorResult":
        _check_schema(value, SCHEMA_RESULT, "EvaluatorResult")
        kwargs = _extract(value, RESULT_FIELDS, "EvaluatorResult",
                          required={"key", "scope", "verdict", "evaluator"}, default=None)
        result = cls(
            key=_nonempty_str(kwargs["key"], "key", "EvaluatorResult"),
            scope=_require_enum(kwargs["scope"], "scope", SCOPES, "EvaluatorResult"),
            verdict=_require_enum(kwargs["verdict"], "verdict", VERDICTS, "EvaluatorResult"),
            evaluator=_evaluator_block(kwargs["evaluator"], "EvaluatorResult"),
            score=_num(kwargs["score"], "score", "EvaluatorResult"),
            calibration=_require_enum(kwargs["calibration"], "calibration",
                                       CALIBRATIONS, "EvaluatorResult",
                                       allow_none=True),
            failure_code=kwargs["failure_code"],
            node_id=kwargs["node_id"],
            expected=kwargs["expected"],
            observed=kwargs["observed"],
            comment=kwargs["comment"],
        )
        evidence = kwargs["evidence"]
        if evidence is not None:
            if not isinstance(evidence, list) or not evidence \
                    or not all(isinstance(item, str) and item for item in evidence):
                raise ContractError(
                    "EvaluatorResult.evidence 提供时必须是非空字符串数组")
            result.evidence = list(evidence)
        if result.verdict == "fail":
            # Round 1 验收：每个 fail 结果都有 key、failureCode、evidence、evaluator 版本
            if not result.failure_code:
                raise ContractError(
                    "verdict=fail 必须携带 failureCode（机器可处理的失败码）")
            if not result.evidence:
                raise ContractError(
                    "verdict=fail 必须携带非空 evidence（不能只给结论不给证据）")
        if result.failure_code is not None and not isinstance(result.failure_code, str):
            raise ContractError("EvaluatorResult.failureCode 必须是字符串")
        result.extra = kwargs["extra"]
        return result


def _require_enum(value: Any, field: str, allowed: tuple, what: str,
                  allow_none: bool = False) -> Optional[str]:
    if value is None:
        if allow_none:
            return None
        raise ContractError(f"{what}.{field} 缺失")
    if value not in allowed:
        raise ContractError(f"{what}.{field} 必须是 {allowed} 之一，实际 {value!r}")
    return value


def _evaluator_block(value: Any, what: str) -> dict:
    if not isinstance(value, dict):
        raise ContractError(f"{what}.evaluator 必须是对象")
    name = _nonempty_str(value.get("name"), "evaluator.name", what)
    version = _nonempty_str(value.get("version"), "evaluator.version", what)
    return {"name": name, "version": version}


# ── 模块级解析入口（后续 Runner 只走这里，不碰各 evaluator 私有结构）──

def parse_eval_case(value: dict, *, require_schema: bool = True) -> EvalCase:
    return EvalCase.from_dict(value, require_schema=require_schema)


def parse_eval_run(value: dict) -> EvalRun:
    return EvalRun.from_dict(value)


def parse_evaluator_result(value: dict) -> EvaluatorResult:
    return EvaluatorResult.from_dict(value)


def parse_artifact_ref(value: dict) -> ArtifactRef:
    return ArtifactRef.from_dict(value)


def roundtrip(obj) -> Any:
    """dumps→loads 往返；公共结构 JSON round-trip 不丢字段的自检入口。"""
    return type(obj).from_dict(stable_roundtrip(obj.to_dict()))
