"""通用工具函数：时间戳、ID 生成、JSON 序列化，以及流程变量模板解析。

模板语法约定（测试流程的动作参数、MES 报文映射等大量使用）：
- ``${变量}`` / ``${a.b.0}``：从上下文按点路径取值，路径段支持字典键和列表下标；
- 整个字符串恰好是一个占位符时保留原始类型（数字/布尔/对象），否则替换为字符串拼接；
- 裸路径（不含 ${} 的 ``flash_result.ok`` 形式）仅在断言取值时启用，见 ``resolve_value``。
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    """返回 UTC 当前时间的 ISO-8601 字符串（毫秒精度，Z 后缀），全系统统一的时间格式。"""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    """生成带业务前缀的全局唯一 ID，如 ``run_9f3c...``、``rpt_...``，便于日志辨认。"""
    return f"{prefix}_{uuid.uuid4().hex}"


def json_dumps(value: Any) -> str:
    """紧凑 JSON 序列化：保留中文（ensure_ascii=False）、无空白、非标准类型兜底为字符串。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


# ${...} 占位符；花括号内不允许再嵌套花括号
_TEMPLATE_PATTERN = re.compile(r"\$\{([^{}]+)\}")
# 裸变量路径：标识符加若干点分段（如 flash_result.ok），用于断言的免 ${} 取值
_BARE_PATH_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*")


def resolve_path(context: dict[str, Any], path: str) -> Any:
    """按点路径从嵌套上下文中取值；路径不存在时抛 KeyError，由调用方决定兜底策略。"""
    value: Any = context
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise KeyError(path)
    return value


def render_template(value: Any, context: dict[str, Any]) -> Any:
    """递归渲染模板：dict/list 逐元素处理；字符串按 ${} 占位符替换。

    整个字符串是单个占位符时返回原始类型（保留数字/布尔/对象）；
    混合文本中的占位符替换为字符串；路径解析失败时保留占位符原文（混排）
    或返回 None（独占整串），避免静默产出错误数据。
    """
    if isinstance(value, dict):
        return {key: render_template(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render_template(item, context) for item in value]
    if not isinstance(value, str):
        return value

    stripped = value.strip()
    exact = _TEMPLATE_PATTERN.fullmatch(stripped)
    if exact:
        try:
            return resolve_path(context, exact.group(1).strip())
        except KeyError:
            return None

    def replace(match: re.Match[str]) -> str:
        try:
            resolved = resolve_path(context, match.group(1).strip())
        except KeyError:
            return match.group(0)
        if resolved is None:
            return ""
        if isinstance(resolved, (dict, list)):
            return json.dumps(resolved, ensure_ascii=False)
        return str(resolved)

    return _TEMPLATE_PATTERN.sub(replace, value)


def resolve_value(value: Any, context: dict[str, Any]) -> Any:
    """断言取值：裸变量路径（如 flash_result.ok）能解析时按变量取值，否则按 ${} 模板或字面量处理。"""
    if isinstance(value, str) and "${" not in value:
        candidate = value.strip()
        if _BARE_PATH_PATTERN.fullmatch(candidate):
            try:
                return resolve_path(context, candidate)
            except KeyError:
                pass
    return render_template(value, context)
