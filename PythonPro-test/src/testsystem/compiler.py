"""v1（步骤式）流程编译器：校验 + 规范化低代码流程 JSON，并分派 v2 控制流流程。

保存流程前必须通过编译：
- 编译失败抛出 ``ScriptValidationError``，携带全部错误（前端逐条展示）；
- 编译成功返回规范化后的定义（补默认值、裁剪空白），随后固化版本存入 scripts 表；
- ``schema_version == 2`` 的流程转交 control_compiler（条件/循环/并行/子流程）。
支持的动作类型、断言操作符、协议清单都在这里定义，是前后端共用的协议字典。
"""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


# 支持的通讯协议（与 protocols.py 的驱动一一对应）
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
# 动作类型：收发/执行设备操作/变量赋值/断言/延时/固件烧录
ACTION_TYPES = {"request", "send", "receive", "execute", "set", "assert", "sleep", "flash"}
# 断言操作符（assert 动作的判定方式）
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
# 步骤/节点 ID：字母开头，≤40 位
ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
# 变量名：标准标识符
VARIABLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# 工站编号：与 MES 主数据 code 同规则
STATION_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ScriptValidationError(ValueError):
    """编译失败：errors 为逐条错误描述（含字段路径），整体消息用中文分号连接。"""

    def __init__(self, errors: list[str]):
        super().__init__("；".join(errors))
        self.errors = errors


def _normalize_max_retests(script: dict[str, Any], errors: list[str]) -> None:
    """校验并规范化流程级失败复测次数（max_retests 字段）。

    规则：字段可缺省（缺省时执行端回退到工位策略里的全局 max_retests）；
    填写时必须是 0-20 的整数，0 表示该流程失败后不允许复测。
    v1（步骤式）与 v2（控制流）编译器共用本函数，保证两种流程语义一致。
    """
    value = script.get("max_retests")
    if value is None or value == "":
        # 未配置：移除空值字段，执行时回退到工位策略
        script.pop("max_retests", None)
        return
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 20:
        errors.append("max_retests 必须是 0-20 的整数（留空表示跟随工位策略）")
        return
    script["max_retests"] = value


# 首次启动写入空库的演示流程：覆盖 SN 核验、版本检查、区间判定、自检四种典型场景
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
    """数值范围校验（拒绝布尔），错误信息带字段路径。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
        errors.append(f"{label} 必须是 {minimum}~{maximum} 之间的数字")


def _validate_action(action: Any, path: str, errors: list[str]) -> None:
    """按动作类型逐项校验字段；path 为字段路径（如 steps[0].actions[1]），用于错误定位。"""
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
        # truthy/exists 只需实际值，其余操作符必须有期望值
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
    elif action_type == "flash":
        firmware = action.get("firmware")
        if not isinstance(firmware, str) or not firmware.strip():
            errors.append(f"{path}.firmware 不能为空，需填写已登记的固件名称或 ID")
        elif "${" not in firmware:
            # 固定固件名（非模板）在编译期就检查固件仓，尽早暴露配置错误；
            # 含 ${} 的留到执行时按变量解析后再校验
            from .flasher import FlashError, find_firmware

            try:
                find_firmware(firmware.strip())
            except FlashError as exc:
                errors.append(f"{path}.firmware {exc}")
        if "save_as" in action and not VARIABLE_PATTERN.fullmatch(str(action["save_as"])):
            errors.append(f"{path}.save_as 不是合法变量名")
        options = action.get("options", {})
        if not isinstance(options, dict):
            errors.append(f"{path}.options 必须是 JSON 对象")
        else:
            for key in ("erase", "verify", "reset"):
                if key in options and not isinstance(options[key], bool):
                    errors.append(f"{path}.options.{key} 必须是布尔值")
            if "timeout_ms" in options:
                _number(options["timeout_ms"], f"{path}.options.timeout_ms", errors, 1, 600000)
        if "variables" in action and not isinstance(action["variables"], dict):
            errors.append(f"{path}.variables 必须是 JSON 对象")


def compile_script(source: dict[str, Any]) -> dict[str, Any]:
    """编译入口：校验并规范化流程定义，失败抛 ScriptValidationError（含全部错误）。

    v2 控制流流程分派给 control_compiler；v1 步骤式流程逐项校验步骤与动作，
    通过后补默认值（超时/重试/失败策略）并裁剪空白，返回可固化的定义。
    """
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
    station_code = str(script.get("station_code", "")).strip()
    if station_code and not STATION_CODE_PATTERN.fullmatch(station_code):
        errors.append("station_code 仅允许字母、数字、点、下划线、连字符，且以字母或数字开头")
    _normalize_max_retests(script, errors)
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

    # 规范化：裁剪空白、补默认值，保证执行端读到的字段齐全
    script["schema_version"] = 1
    script["name"] = str(script["name"]).strip()
    script["product_model"] = str(script["product_model"]).strip()
    script["station_code"] = station_code
    script.setdefault("variables", {})
    for step in script["steps"]:
        step.setdefault("timeout_ms", 5000)
        step.setdefault("retries", 0)
        step.setdefault("on_fail", "stop")
    return script
