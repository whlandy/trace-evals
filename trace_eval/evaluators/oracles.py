#!/usr/bin/env python3
"""Round 4：业务 Oracle —— 独立证明「被测系统最终达到业务目标」（C3 层）。

首批三类（设计 §8.3 / Round 4 代码修改清单）：

- ``api-json``     验证后端资源最终状态：对**已录制的**响应 JSON 做 JSONPath 断言；
- ``ui-state``     验证 UI 文本、属性或控件状态：对**已录制的** UI 状态证据做断言；
- ``file-content`` 验证文件或配置落盘：对受控相对路径内容做断言。

沙箱约束（验收：Oracle 不允许任意 shell 或未登记的网络目标）：

- 本模块**不 import** subprocess / socket / urllib / requests / os.system：
  没有进程、没有网络、没有解释执行 —— 结构上不可能执行任意 shell；
- 一切证据都是**已录制的 Artifact**（run 目录内的相对路径，不允许
  ``..`` 逃逸、不允许绝对路径）；
- ``api-json.target`` 若声明，必须命中**登记**的目标注册表
  （``ORACLE_TARGET_REGISTRY``）——未登记的网络目标在**配置解析**阶段即拒绝，
  根本走不到执行。

Oracle 输出统一 EvaluatorResult（Round 1 协议）：

- 断言通过 → pass；不满足 → fail（failureCode + expected/observed + 可复查证据）；
- 证据缺失（未录制该状态）→ inconclusive，**不得自动按通过处理**；
- 配置违例（OracleConfigError）→ inconclusive 并带原因（配置错误不是业务失败）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from trace_eval.contracts import EvaluatorResult

EVALUATOR_NAME = "oracle"
EVALUATOR_VERSION = "1.0.0"

SCHEMA_ORACLE = "trace-evals.oracle/v1"
ORACLE_TYPES = ("api-json", "ui-state", "file-content")

# 登记的网络目标（离线回放模式下只有一个：本地录制响应）。
# 新增目标必须显式登记——未登记 = 配置违例，在解析阶段拒绝。
ORACLE_TARGET_REGISTRY = {
    "api.local": "仅离线：已录制的 API 响应，不产生真实网络调用",
}


class OracleConfigError(ValueError):
    """Oracle 配置违例（未登记目标、路径逃逸、类型错误……）。"""


@dataclass
class OracleConfig:
    schema: str
    type: str
    name: str
    evidence: str          # run 目录内相对路径（已录制证据 Artifact）
    expect: dict
    json_path: str | None = None
    selector: str | None = None
    target: str | None = None
    extra: dict | None = None


def parse_oracle_config(value: dict) -> OracleConfig:
    if not isinstance(value, dict):
        raise OracleConfigError("Oracle 配置必须是 JSON 对象")
    schema = value.get("schema")
    if schema != SCHEMA_ORACLE:
        family = SCHEMA_ORACLE.rsplit("/", 1)[0]
        if isinstance(schema, str) and schema.rsplit("/", 1)[0] == family:
            raise OracleConfigError(f"未知主版本 {schema!r}（期望 {SCHEMA_ORACLE!r}）")
        raise OracleConfigError(f"缺少或无效 schema {schema!r}")
    otype = value.get("type")
    if otype not in ORACLE_TYPES:
        raise OracleConfigError(
            f"type 必须是 {ORACLE_TYPES} 之一，实际 {otype!r}")
    name = value.get("name")
    if not isinstance(name, str) or not name:
        raise OracleConfigError("name 必须是非空字符串")
    evidence = value.get("evidence")
    if not isinstance(evidence, str) or not evidence \
            or evidence.startswith("/") or ".." in evidence.split("/"):
        raise OracleConfigError(
            f"evidence 必须是 run 目录内相对路径：{evidence!r}")
    expect = value.get("expect")
    if not isinstance(expect, dict) or not expect:
        raise OracleConfigError("expect 必须是非空对象")
    known_expect = {"equals", "contains", "present", "jsonPath", "attribute",
                    "visible"}
    unknown = set(expect) - known_expect
    if unknown:
        raise OracleConfigError(f"expect 含未知键：{sorted(unknown)}")
    json_path = value.get("jsonPath")
    if json_path is not None and not isinstance(json_path, str):
        raise OracleConfigError("jsonPath 必须是字符串")
    selector = value.get("selector")
    if selector is not None and not isinstance(selector, str):
        raise OracleConfigError("selector 必须是字符串")
    target = value.get("target")
    if target is not None:
        if not isinstance(target, str) or target not in ORACLE_TARGET_REGISTRY:
            raise OracleConfigError(
                f"未登记的网络目标 {target!r}：只允许 {sorted(ORACLE_TARGET_REGISTRY)}"
                "（新增目标必须显式登记）")
    known = {"schema", "type", "name", "evidence", "expect", "jsonPath",
             "selector", "target"}
    extra = {k: v for k, v in value.items() if k not in known}
    return OracleConfig(schema=schema, type=otype, name=name, evidence=evidence,
                         expect=expect, json_path=json_path, selector=selector,
                         target=target, extra=extra or None)


# ── 内部工具 ──────────────────────────────────────────────────────


def _safe_path(base_dir: Path, rel: str) -> Path:
    base = Path(base_dir).resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base):
        raise OracleConfigError(f"证据路径逃逸 run 目录：{rel!r}")
    return target


def _load_evidence(base_dir: Path, rel: str) -> dict | list | None:
    path = _safe_path(base_dir, rel)
    if not path.is_file():
        return None  # 证据缺失 → 由调用方报 inconclusive
    return json.loads(path.read_text(encoding="utf-8"))


def _json_path_walk(data, expression: str):
    """最小点号 JSONPath：a.b.0.c。缺路径抛 KeyError。"""
    node = data
    for segment in expression.split("."):
        if not segment:
            raise KeyError(f"空路径段（{expression!r}）")
        if isinstance(node, list):
            if not segment.isdigit():
                raise KeyError(f"下标必须是数字：{segment!r}")
            index = int(segment)
            if index >= len(node):
                raise KeyError(f"下标越界：{index} / {len(node)}")
            node = node[index]
        elif isinstance(node, dict):
            if segment not in node:
                raise KeyError(segment)
            node = node[segment]
        else:
            raise KeyError(f"无法深入 {type(node).__name__}：{segment!r}")
    return node


def _compare(expect: dict, observed) -> bool:
    if "equals" in expect:
        return observed == expect["equals"]
    if "contains" in expect:
        if not isinstance(observed, str):
            return False
        return expect["contains"] in observed
    if "present" in expect:
        return (observed is not None) == bool(expect["present"])
    return False


def _result(cfg: OracleConfig, verdict: str, **kw) -> EvaluatorResult:
    base = dict(key=f"oracle.{cfg.type}.{cfg.name}", scope="case",
                verdict=verdict,
                calibration="deterministic",
                evaluator={"name": EVALUATOR_NAME, "version": EVALUATOR_VERSION},
                expected=cfg.expect)
    if verdict == "fail" and not kw.get("failure_code"):
        base["failure_code"] = "ORACLE_MISMATCH"
    if verdict == "fail" and not kw.get("evidence"):
        base["evidence"] = [f"artifact:{cfg.evidence}"]
    return EvaluatorResult(**base, **kw)


# ── 三类 Oracle ───────────────────────────────────────────────────


def evaluate_api_json(cfg: OracleConfig, run_dir: Path) -> EvaluatorResult:
    """后端资源最终状态：对已录制响应 JSON 做断言。"""
    data = _load_evidence(run_dir, cfg.evidence)
    if data is None:
        return _result(cfg, "inconclusive",
                       comment=f"缺少已录制的 API 响应证据：{cfg.evidence}")
    expression = cfg.json_path or ""
    try:
        observed = _json_path_walk(data, expression) if expression else data
    except KeyError as error:
        return _result(cfg, "inconclusive",
                       comment=f"响应中不存在该路径（证据不足）：{error.args[0] if error.args else expression}")
    ok = _compare(cfg.expect, observed)
    if ok:
        return _result(cfg, "pass", observed=observed,
                       evidence=[f"artifact:{cfg.evidence}"
                                  + (f"#jsonPath={expression}" if expression else "")])
    return _result(cfg, "fail",
                   failure_code="ORACLE_API_STATE_WRONG",
                   observed=observed,
                   evidence=[f"artifact:{cfg.evidence}"
                              + (f"#jsonPath={expression}" if expression else "")
                              + f" → observed={json.dumps(observed, ensure_ascii=False)}"
                              f" expected={json.dumps(cfg.expect, ensure_ascii=False)}"])


def evaluate_ui_state(cfg: OracleConfig, run_dir: Path) -> EvaluatorResult:
    """UI 文本/属性/控件状态：对已录制的 UI 状态证据做断言。"""
    data = _load_evidence(run_dir, cfg.evidence)
    if data is None:
        return _result(cfg, "inconclusive",
                       comment=f"缺少已录制的 UI 状态证据：{cfg.evidence}")
    if cfg.selector is not None and data.get("selector") != cfg.selector:
        return _result(cfg, "fail",
                       failure_code="ORACLE_UI_SELECTOR_MISMATCH",
                       observed={"selector": data.get("selector")},
                       evidence=[f"artifact:{cfg.evidence} 的 selector 与 Oracle 不符"
                                 f"（expected={cfg.selector}）"])
    expect = cfg.expect
    if "visible" in expect:
        observed = bool(data.get("visible"))
        ok = observed == bool(expect["visible"])
    else:
        attribute = expect.get("attribute")
        observed = (data.get("attributes") or {}).get(attribute)
        ok = _compare(expect, observed)
    if ok:
        return _result(cfg, "pass", observed=observed,
                       evidence=[f"artifact:{cfg.evidence}"])
    return _result(cfg, "fail",
                   failure_code="ORACLE_UI_STATE_WRONG",
                   observed=observed,
                   evidence=[f"artifact:{cfg.evidence} → observed={observed!r}"])


def evaluate_file_content(cfg: OracleConfig, run_dir: Path) -> EvaluatorResult:
    """文件/配置落盘：对受控相对路径内容做断言。"""
    path = _safe_path(run_dir, cfg.evidence)
    if not path.is_file():
        return _result(cfg, "inconclusive",
                       comment=f"缺少落盘证据文件：{cfg.evidence}")
    text = path.read_text(encoding="utf-8", errors="replace")
    expect = cfg.expect
    if cfg.json_path is not None:
        try:
            observed = _json_path_walk(json.loads(text), cfg.json_path)
        except (KeyError, json.JSONDecodeError) as error:
            return _result(cfg, "inconclusive",
                             comment=f"文件中不存在该字段（证据不足）：{error}")
        ok = _compare(expect, observed)
    elif "equals" in expect:
        observed, ok = text, text == expect["equals"]
    elif "contains" in expect:
        observed, ok = f"len={len(text)}", expect["contains"] in text
    else:
        observed, ok = f"len={len(text)}", "present" in expect and expect["present"] is True
    if ok:
        return _result(cfg, "pass", observed=observed,
                       evidence=[f"artifact:{cfg.evidence}"])
    return _result(cfg, "fail",
                   failure_code="ORACLE_FILE_CONTENT_WRONG",
                   observed=observed,
                   evidence=[f"artifact:{cfg.evidence} 内容与期望不符"])


_DISPATCH = {
    "api-json": evaluate_api_json,
    "ui-state": evaluate_ui_state,
    "file-content": evaluate_file_content,
}


def evaluate_oracles(case, run_dir: Path) -> list[EvaluatorResult]:
    """执行 Case 声明的全部 Oracle；配置违例记 inconclusive（不是业务失败）。"""
    results: list[EvaluatorResult] = []
    for raw in (case.oracles or []):
        if isinstance(raw, OracleConfig):
            cfg = raw
        else:
            try:
                cfg = parse_oracle_config(raw)
            except OracleConfigError as error:
                results.append(EvaluatorResult(
                    key=f"oracle.config-error-{len(results)}",
                    scope="case", verdict="inconclusive",
                    calibration="deterministic",
                    comment=f"Oracle 配置错误：{error}",
                    evaluator={"name": EVALUATOR_NAME,
                               "version": EVALUATOR_VERSION}))
                continue
        try:
            results.append(_DISPATCH[cfg.type](cfg, run_dir))
        except OracleConfigError as error:
            results.append(EvaluatorResult(
                key=f"oracle.{cfg.type}.{cfg.name}",
                scope="case", verdict="inconclusive",
                calibration="deterministic",
                comment=f"Oracle 执行期配置错误：{error}",
                evaluator={"name": EVALUATOR_NAME,
                           "version": EVALUATOR_VERSION}))
    return results
