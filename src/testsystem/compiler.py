from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


SUPPORTED_PROTOCOLS = {
    "simulator",
    "serial",
    "can",
    "tcp",
    "modbus",
    "bluetooth",
    "wifi",
    "mqtt",
    "jlink",
}
ACTION_TYPES = {"request", "send", "receive", "execute", "set", "assert", "sleep"}
ASSERT_OPERATORS = {
    "equals",
    "not_equals",
    "contains",
    "not_contains",
    "regex",
    "gt",
    "gte",
    "lt",
    "lte",
    "between",
    "truthy",
    "exists",
}
ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
VARIABLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ScriptValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("；".join(errors))
        self.errors = errors


DEFAULT_SCRIPT: dict[str, Any] = {
    "schema_version": 1,
    "name": "通用电子产品出厂测试",
    "product_model": "DEMO-100",
    "variables": {},
    "steps": [
        {
            "id": "sn_binding",
            "name": "SN 绑定核验",
            "protocol": "simulator",
            "timeout_ms": 5000,
            "retries": 0,
            "on_fail": "stop",
            "actions": [
                {"type": "request", "data": "AT+SN?", "encoding": "text", "save_as": "device_sn"},
                {"type": "assert", "actual": "${device_sn}", "operator": "equals", "expected": "${sn}"},
            ],
        },
        {
            "id": "firmware",
            "name": "固件版本检查",
            "protocol": "simulator",
            "timeout_ms": 5000,
            "retries": 1,
            "on_fail": "stop",
            "actions": [
                {"type": "request", "data": "AT+VERSION?", "encoding": "text", "save_as": "firmware"},
                {"type": "assert", "actual": "${firmware}", "operator": "equals", "expected": "1.0.0"},
            ],
        },
        {
            "id": "voltage",
            "name": "电源电压检查",
            "protocol": "simulator",
            "timeout_ms": 5000,
            "retries": 0,
            "on_fail": "continue",
            "actions": [
                {"type": "request", "data": "AT+VOLTAGE?", "encoding": "text", "save_as": "voltage"},
                {"type": "assert", "actual": "${voltage}", "operator": "between", "expected": [11.5, 12.5]},
            ],
        },
        {
            "id": "self_test",
            "name": "产品功能自检",
            "protocol": "simulator",
            "timeout_ms": 8000,
            "retries": 1,
            "on_fail": "stop",
            "actions": [
                {"type": "request", "data": "AT+SELFTEST?", "encoding": "text", "save_as": "self_test"},
                {"type": "assert", "actual": "${self_test}", "operator": "equals", "expected": "PASS"},
            ],
        },
    ],
}


def _number(value: Any, label: str, errors: list[str], minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
        errors.append(f"{label} 必须是 {minimum}~{maximum} 之间的数字")


def _validate_action(action: Any, path: str, errors: list[str]) -> None:
    if not isinstance(action, dict):
        errors.append(f"{path} 必须是对象")
        return
    action_type = action.get("type")
    if action_type not in ACTION_TYPES:
        errors.append(f"{path}.type 不支持：{action_type!r}")
        return

    if action_type in {"request", "send"}:
        if not isinstance(action.get("data"), (str, int, float)):
            errors.append(f"{path}.data 不能为空")
        if action.get("encoding", "text") not in {"text", "hex", "json"}:
            errors.append(f"{path}.encoding 仅支持 text、hex、json")
        if "save_as" in action and not VARIABLE_PATTERN.fullmatch(str(action["save_as"])):
            errors.append(f"{path}.save_as 不是合法变量名")
    elif action_type == "receive":
        if not VARIABLE_PATTERN.fullmatch(str(action.get("save_as", ""))):
            errors.append(f"{path}.save_as 必须填写合法变量名")
        _number(action.get("timeout_ms", 1000), f"{path}.timeout_ms", errors, 1, 300000)
        _number(action.get("size", 4096), f"{path}.size", errors, 1, 1048576)
    elif action_type == "execute":
        if not str(action.get("operation", "")).strip():
            errors.append(f"{path}.operation 不能为空")
        if not isinstance(action.get("params", {}), dict):
            errors.append(f"{path}.params 必须是 JSON 对象")
    elif action_type == "set":
        if not VARIABLE_PATTERN.fullmatch(str(action.get("variable", ""))):
            errors.append(f"{path}.variable 不是合法变量名")
    elif action_type == "assert":
        if "actual" not in action:
            errors.append(f"{path}.actual 不能为空")
        operator = action.get("operator")
        if operator not in ASSERT_OPERATORS:
            errors.append(f"{path}.operator 不支持：{operator!r}")
        if operator not in {"truthy", "exists"} and "expected" not in action:
            errors.append(f"{path}.expected 不能为空")
        if operator == "between":
            expected = action.get("expected")
            if not isinstance(expected, list) or len(expected) != 2:
                errors.append(f"{path}.expected 必须是 [下限, 上限]")
            elif any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in expected):
                errors.append(f"{path}.expected 上下限必须是十进制数值")
        if operator == "regex":
            try:
                re.compile(str(action.get("expected", "")))
            except re.error as exc:
                errors.append(f"{path}.expected 正则无效：{exc}")
    elif action_type == "sleep":
        _number(action.get("duration_ms"), f"{path}.duration_ms", errors, 0, 60000)


def compile_script(source: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(source, dict):
        raise ScriptValidationError(["脚本根节点必须是 JSON 对象"])
    if source.get("schema_version") == 2:
        from .control_compiler import compile_control_script

        return compile_control_script(source)

    script = deepcopy(source)
    errors: list[str] = []

    if script.get("schema_version", 1) != 1:
        errors.append("schema_version 当前仅支持 1")
    if not str(script.get("name", "")).strip():
        errors.append("脚本名称不能为空")
    if not str(script.get("product_model", "")).strip():
        errors.append("产品型号不能为空")
    if not isinstance(script.get("variables", {}), dict):
        errors.append("variables 必须是 JSON 对象")
    steps = script.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("至少需要一个测试项")
        steps = steps if isinstance(steps, list) else []
    if len(steps) > 100:
        errors.append("单个脚本最多 100 个测试项")

    seen_ids: set[str] = set()
    for index, step in enumerate(steps):
        path = f"steps[{index}]"
        if not isinstance(step, dict):
            errors.append(f"{path} 必须是对象")
            continue
        step_id = step.get("id")
        if not isinstance(step_id, str) or not ID_PATTERN.fullmatch(step_id):
            errors.append(f"{path}.id 需以字母开头，仅含字母、数字、下划线或连字符")
        elif step_id in seen_ids:
            errors.append(f"{path}.id 重复：{step_id}")
        else:
            seen_ids.add(step_id)
        if not str(step.get("name", "")).strip():
            errors.append(f"{path}.name 不能为空")
        if step.get("protocol") not in SUPPORTED_PROTOCOLS:
            errors.append(f"{path}.protocol 不受支持")
        _number(step.get("timeout_ms", 5000), f"{path}.timeout_ms", errors, 1, 300000)
        _number(step.get("retries", 0), f"{path}.retries", errors, 0, 10)
        if step.get("on_fail", "stop") not in {"stop", "continue"}:
            errors.append(f"{path}.on_fail 仅支持 stop 或 continue")
        actions = step.get("actions")
        if not isinstance(actions, list) or not actions:
            errors.append(f"{path}.actions 至少需要一个动作")
            actions = actions if isinstance(actions, list) else []
        if len(actions) > 50:
            errors.append(f"{path} 最多 50 个动作")
        for action_index, action in enumerate(actions):
            _validate_action(action, f"{path}.actions[{action_index}]", errors)

    if errors:
        raise ScriptValidationError(errors)

    script["schema_version"] = 1
    script["name"] = str(script["name"]).strip()
    script["product_model"] = str(script["product_model"]).strip()
    script.setdefault("variables", {})
    for step in script["steps"]:
        step.setdefault("timeout_ms", 5000)
        step.setdefault("retries", 0)
        step.setdefault("on_fail", "stop")
    return script
