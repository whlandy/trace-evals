"""独立于 recorder trace schema 的扩展业务 oracle 规格。"""

from __future__ import annotations

from typing import Any

SCHEMA = "trace-eval.oracle-spec/v1"
SCHEMA_V2 = "trace-eval.oracle-spec/v2"
SCHEMA_V3 = "trace-eval.oracle-spec/v3"
PERSISTENCE_METHODS = {"reload", "api", "database", "requery", "new_session"}


def validate_oracle_spec(spec: dict[str, Any] | None, *, node_ids: set[str]) -> dict:
    if spec is None:
        return {"schema": SCHEMA_V2, "nodes": {}, "flow": {}}
    if not isinstance(spec, dict) or spec.get("schema") not in {SCHEMA, SCHEMA_V2, SCHEMA_V3}:
        raise ValueError("不支持的 oracle spec schema")
    expected_top = ({"schema", "nodes"} if spec["schema"] == SCHEMA
                    else {"schema", "nodes", "flow"})
    if set(spec) != expected_top:
        raise ValueError(f"oracle spec 顶层字段必须严格为 {sorted(expected_top)}")
    if not isinstance(spec["nodes"], dict):
        raise ValueError("oracle spec.nodes 必须是对象")
    for node_id, node_spec in spec["nodes"].items():
        if node_id not in node_ids:
            raise ValueError(f"oracle spec 引用了不存在的 nodeId：{node_id}")
        if not isinstance(node_spec, dict) or set(node_spec) != {"persistence"}:
            raise ValueError(f"oracle spec {node_id} 只支持 persistence")
        persistence = node_spec["persistence"]
        required = {"method", "expected"}
        allowed = required | {"maxDelayMs"}
        if not isinstance(persistence, dict) or not required <= set(persistence) or not set(persistence) <= allowed:
            raise ValueError(f"oracle spec {node_id}.persistence 字段不合法")
        if persistence["method"] not in PERSISTENCE_METHODS:
            raise ValueError(f"未知 persistence method：{persistence['method']!r}")
        delay = persistence.get("maxDelayMs")
        if delay is not None and (isinstance(delay, bool) or not isinstance(delay, (int, float))
                                  or delay < 0):
            raise ValueError("maxDelayMs 必须是非负数")
    if spec["schema"] in {SCHEMA_V2, SCHEMA_V3}:
        flow = spec["flow"]
        allowed = {"maxExtraSteps", "maxTotalRetries", "maxDurationMs"}
        if spec["schema"] == SCHEMA_V3:
            allowed |= {"optionalNodeIds", "orderConstraints", "allowedExtraActions"}
        if not isinstance(flow, dict) or not set(flow) <= allowed:
            raise ValueError("oracle spec.flow 字段不合法")
        for key, value in flow.items():
            if key in {"optionalNodeIds", "orderConstraints", "allowedExtraActions"}:
                continue
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or value < 0):
                raise ValueError(f"flow.{key} 必须是非负数")
        optional = flow.get("optionalNodeIds", [])
        if (not isinstance(optional, list)
                or not all(isinstance(value, str) and value in node_ids for value in optional)
                or len(optional) != len(set(optional))):
            raise ValueError("flow.optionalNodeIds 必须是唯一且存在的 nodeId 数组")
        allowed_actions = flow.get("allowedExtraActions", [])
        if (not isinstance(allowed_actions, list)
                or not all(isinstance(value, str) and value.strip() for value in allowed_actions)
                or len(allowed_actions) != len(set(allowed_actions))):
            raise ValueError("flow.allowedExtraActions 必须是唯一非空 action 字符串数组")
        constraints = flow.get("orderConstraints", [])
        if not isinstance(constraints, list):
            raise ValueError("flow.orderConstraints 必须是数组")
        seen = set()
        for item in constraints:
            if (not isinstance(item, dict) or set(item) != {"before", "after"}
                    or not all(isinstance(value, str) for value in item.values())
                    or item["before"] not in node_ids or item["after"] not in node_ids
                    or item["before"] == item["after"]):
                raise ValueError("flow.orderConstraints 必须引用两个不同且存在的 nodeId")
            pair = (item["before"], item["after"])
            if pair in seen:
                raise ValueError("flow.orderConstraints 不得重复")
            seen.add(pair)
        # Reject impossible specifications, including cycles through omitted optional nodes.
        remaining = set(seen)
        while remaining:
            incoming = {after for _, after in remaining}
            roots = {before for before, _ in remaining} - incoming
            if not roots:
                raise ValueError("flow.orderConstraints 不得成环")
            remaining = {(before, after) for before, after in remaining if before not in roots}
    return ({**spec, "flow": {}} if spec["schema"] == SCHEMA else spec)
