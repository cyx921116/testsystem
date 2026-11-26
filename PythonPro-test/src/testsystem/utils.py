from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


_TEMPLATE_PATTERN = re.compile(r"\$\{([^{}]+)\}")
_BARE_PATH_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*")


def resolve_path(context: dict[str, Any], path: str) -> Any:
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
