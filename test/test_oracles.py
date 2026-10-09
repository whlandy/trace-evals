"""Round 4 验收：业务 Oracle（api-json / ui-state / file-content）。

逐条守设计文档 Round 4 的验收标准（Oracle 部分）：

- Oracle 失败保留 observed、expected 和可复查证据；
- Oracle 不允许任意 shell 或未登记的网络目标（配置阶段即拒绝 +
  模块级结构守卫：无 subprocess/socket/解释执行）；
- 证据缺失 → inconclusive，不得自动按通过处理。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trace_eval.evaluators.oracles import (
    OracleConfigError, evaluate_oracles, parse_oracle_config,
)

HERE = Path(__file__).resolve().parent / "fixtures" / "contracts"


def _run_dir(tmp_path: Path, artifacts: dict) -> Path:
    art = tmp_path / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    for name, content in artifacts.items():
        (art / name).write_text(
            content if isinstance(content, str)
            else json.dumps(content, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _oracle(**over) -> dict:
    base = {"schema": "trace-evals.oracle/v1", "type": "api-json",
            "name": "policy", "evidence": "artifacts/response.json",
            "jsonPath": "state", "expect": {"equals": "enabled"}}
    base.update(over)
    return base


# ── 配置严格解析：未登记目标 / 路径逃逸 / 版本 ────────────────────


def test_config_rejects_unregistered_network_target():
    with pytest.raises(OracleConfigError, match="未登记"):
        parse_oracle_config(_oracle(target="https://prod.internal/api"))


def test_config_rejects_registered_target():
    parse_oracle_config(_oracle(target="api.local"))


def test_config_rejects_evidence_path_escape():
    with pytest.raises(OracleConfigError, match="相对路径"):
        parse_oracle_config(_oracle(evidence="../../etc/passwd"))
    with pytest.raises(OracleConfigError, match="相对路径"):
        parse_oracle_config(_oracle(evidence="/etc/passwd"))


def test_config_rejects_unknown_major_version_and_bad_type():
    with pytest.raises(OracleConfigError, match="未知主版本"):
        parse_oracle_config(_oracle(schema="trace-evals.oracle/v2"))
    with pytest.raises(OracleConfigError, match="type"):
        parse_oracle_config(_oracle(type="shell-cmd"))
    with pytest.raises(OracleConfigError, match="expect"):
        parse_oracle_config(_oracle(expect={}))
    with pytest.raises(OracleConfigError, match="未知键"):
        parse_oracle_config(_oracle(expect={"equals": "x", "shell": "rm -rf /"}))


# ── api-json：执行全成功但后端状态错 → fail，且证据齐全 ──────────


def test_api_json_fail_retains_expected_observed_and_evidence(tmp_path):
    run = _run_dir(tmp_path, {"response.json": {"state": "disabled"}})
    results = evaluate_oracles(
        SimpleNamespace(oracles=[_oracle()]), run)
    assert len(results) == 1
    result = results[0]
    assert result.verdict == "fail"
    assert result.failure_code == "ORACLE_API_STATE_WRONG"
    assert result.expected == {"equals": "enabled"}
    assert result.observed == "disabled"
    assert result.evidence and "artifacts/response.json" in result.evidence[0]
    assert result.evaluator["version"]


def test_api_json_pass_and_contains(tmp_path):
    run = _run_dir(tmp_path, {"response.json": {"state": "enabled"}})
    assert evaluate_oracles(
        SimpleNamespace(oracles=[_oracle()]), run)[0].verdict == "pass"
    run2 = _run_dir(tmp_path / "b", {"response.json": {"msg": "policy=on"}})
    result = evaluate_oracles(
        SimpleNamespace(oracles=[_oracle(evidence="artifacts/response.json",
                                        jsonPath="msg",
                                        expect={"contains": "policy"})]),
        run2)[0]
    assert result.verdict == "pass"


def test_api_json_missing_evidence_is_inconclusive(tmp_path):
    run = _run_dir(tmp_path, {})
    result = evaluate_oracles(
        SimpleNamespace(oracles=[_oracle()]), run)[0]
    assert result.verdict == "inconclusive"
    assert result.failure_code is None  # 缺证据不是失败，更不是通过


def test_api_json_bad_jsonpath_is_inconclusive_not_fail(tmp_path):
    run = _run_dir(tmp_path, {"response.json": {"state": "enabled"}})
    result = evaluate_oracles(
        SimpleNamespace(oracles=[_oracle(jsonPath="does/not/exist")]), run)[0]
    assert result.verdict == "inconclusive"


def test_oracle_config_error_becomes_inconclusive_not_fail(tmp_path):
    run = _run_dir(tmp_path, {"response.json": {"state": "enabled"}})
    result = evaluate_oracles(
        SimpleNamespace(oracles=[_oracle(target="https://evil.example")]),
        run)[0]
    assert result.verdict == "inconclusive"
    assert "未登记" in (result.comment or "")


# ── ui-state / file-content ──────────────────────────────────────


def test_ui_state_fail_and_selector_mismatch(tmp_path):
    run = _run_dir(tmp_path, {"ui-state.json":
                              {"selector": 'locator("tag")',
                               "visible": True,
                               "attributes": {"text": "已启用"}}})
    ok = evaluate_oracles(
        SimpleNamespace(oracles=[_oracle(
            type="ui-state", name="tag", evidence="artifacts/ui-state.json",
            selector='locator("tag")', expect={"attribute": "text",
                                               "equals": "已启用"})]), run)[0]
    assert ok.verdict == "pass"
    wrong = evaluate_oracles(
        SimpleNamespace(oracles=[_oracle(
            type="ui-state", name="tag", evidence="artifacts/ui-state.json",
            selector='locator("tag")', expect={"attribute": "text",
                                               "equals": "已停用"})]), run)[0]
    assert wrong.verdict == "fail"
    assert wrong.failure_code == "ORACLE_UI_STATE_WRONG"
    mismatch = evaluate_oracles(
        SimpleNamespace(oracles=[_oracle(
            type="ui-state", name="tag", evidence="artifacts/ui-state.json",
            selector='locator("other")', expect={"visible": True})]), run)[0]
    assert mismatch.verdict == "fail"
    assert mismatch.failure_code == "ORACLE_UI_SELECTOR_MISMATCH"


def test_file_content_jsonpath_and_missing(tmp_path):
    run = _run_dir(tmp_path, {"config.json": {"policy": "on", "level": 2}})
    ok = evaluate_oracles(
        SimpleNamespace(oracles=[_oracle(
            type="file-content", name="cfg",
            evidence="artifacts/config.json", jsonPath="policy",
            expect={"equals": "on"})]), run)[0]
    assert ok.verdict == "pass"
    missing = evaluate_oracles(
        SimpleNamespace(oracles=[_oracle(
            type="file-content", name="cfg",
            evidence="artifacts/absent.json", expect={"contains": "x"})]),
        run)[0]
    assert missing.verdict == "inconclusive"


# ── 结构守卫：不允许任意 shell / 未登记网络 ─────────────────────


def test_oracle_module_has_no_shell_or_network_primitives():
    """AST 级守卫：oracles.py 不得 import/调用 进程、网络或解释执行原语。"""
    import ast
    source = (Path(__file__).resolve().parents[1]
              / "trace_eval" / "evaluators" / "oracles.py").read_text(
                  encoding="utf-8")
    tree = ast.parse(source)
    forbidden_modules = {"subprocess", "socket", "urllib", "requests",
                        "http", "httpx", "aiohttp", "os", "pty"}
    forbidden_calls = {"eval", "exec", "system", "popen", "spawn"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in forbidden_modules, \
                    f"Oracle 模块禁止 import {root}"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in forbidden_modules, \
                f"Oracle 模块禁止 from {node.module} import"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in forbidden_calls, \
                f"Oracle 模块禁止调用 {node.func.id}()（任意 shell/解释执行）"
