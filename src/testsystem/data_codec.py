from __future__ import annotations

import json
import math
import re
import struct
from decimal import Decimal, InvalidOperation
from typing import Any


class DataCodecError(ValueError):
    pass


VALUE_FORMATS = {
    "uint8": "B",
    "int8": "b",
    "uint16": "H",
    "int16": "h",
    "uint32": "I",
    "int32": "i",
    "float32": "f",
    "float64": "d",
}
SEND_FORMATS = {"text", "decimal", "json", "hex"}
RECEIVE_FORMATS = {"text", "decimal", "json", "hex"}


def _decimal_values(data: Any) -> list[Decimal]:
    raw_values = data if isinstance(data, (list, tuple)) else None
    if raw_values is None and isinstance(data, str):
        text = data.strip()
        if not text:
            raise DataCodecError("十进制下发数据不能为空")
        raw_values = [part for part in re.split(r"[,，\s]+", text) if part]
    if raw_values is None:
        raw_values = [data]
    if not raw_values:
        raise DataCodecError("十进制下发数据不能为空")
    values = []
    for raw in raw_values:
        if isinstance(raw, bool):
            raise DataCodecError("布尔值不能作为十进制数值下发")
        try:
            value = Decimal(str(raw).strip())
        except (InvalidOperation, ValueError) as exc:
            raise DataCodecError(f"仅支持十进制数值，无法解析：{raw!r}") from exc
        if not value.is_finite():
            raise DataCodecError("数值不能是 NaN 或无穷大")
        values.append(value)
    return values


def _scaled_numbers(data: Any, scale: Any) -> list[Decimal]:
    try:
        factor = Decimal(str(scale))
    except InvalidOperation as exc:
        raise DataCodecError("数值缩放系数无效") from exc
    if not factor.is_finite():
        raise DataCodecError("数值缩放系数必须是有限数值")
    return [value * factor for value in _decimal_values(data)]


def encode_payload(data: Any, config: dict[str, Any], legacy_encoding: str | None = None) -> bytes:
    charset = str(config.get("encoding", "utf-8"))
    data_format = legacy_encoding or str(config.get("send_format", "text"))
    if data_format == "text":
        return str(data).encode(charset)
    if data_format == "json":
        return json.dumps(data, ensure_ascii=False).encode(charset)
    if data_format == "hex":
        if not isinstance(data, str):
            raise DataCodecError("旧版 HEX 发送数据必须是字符串")
        try:
            return bytes.fromhex(data)
        except ValueError as exc:
            raise DataCodecError(f"旧版 HEX 数据格式错误：{exc}") from exc
    if data_format != "decimal":
        raise DataCodecError(f"不支持的下发格式：{data_format}")
    value_type = str(config.get("send_type", "uint16"))
    byte_order = str(config.get("send_byte_order", "big"))
    _validate_type_and_order(value_type, byte_order)
    numbers = _scaled_numbers(data, config.get("send_scale", 1))
    converted: list[int | float] = []
    if value_type.startswith("float"):
        converted = [float(value) for value in numbers]
    else:
        for value in numbers:
            integer = int(value)
            if value != integer:
                raise DataCodecError(f"{value_type} 不能保存非整数：{value}")
            converted.append(integer)
    try:
        return struct.pack((">" if byte_order == "big" else "<") + VALUE_FORMATS[value_type] * len(converted), *converted)
    except (struct.error, OverflowError) as exc:
        raise DataCodecError(f"数值超出 {value_type} 可表示范围：{exc}") from exc


def decode_payload(data: bytes, config: dict[str, Any], legacy_format: str | None = None) -> Any:
    charset = str(config.get("encoding", "utf-8"))
    data_format = legacy_format or str(config.get("receive_format") or config.get("response_format", "text"))
    if data_format == "hex":
        return data.hex(" ").upper()
    if data_format == "text":
        return data.decode(charset, errors="replace").strip()
    if data_format == "json":
        try:
            return json.loads(data.decode(charset).strip())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataCodecError(f"响应不是合法 JSON：{exc}") from exc
    if data_format != "decimal":
        raise DataCodecError(f"不支持的响应格式：{data_format}")

    value_type = str(config.get("receive_type", "uint16"))
    byte_order = str(config.get("receive_byte_order", "big"))
    _validate_type_and_order(value_type, byte_order)
    offset = _nonnegative_int(config.get("receive_offset", 0), "响应偏移")
    length = _nonnegative_int(config.get("receive_length", 0), "响应截取长度")
    if offset > len(data):
        raise DataCodecError(f"响应偏移 {offset} 超过实际字节数 {len(data)}")
    payload = data[offset:] if length == 0 else data[offset:offset + length]
    item_size = struct.calcsize(VALUE_FORMATS[value_type])
    if not payload or len(payload) % item_size:
        raise DataCodecError(f"响应数据 {len(payload)} 字节，不能按 {value_type}（{item_size} 字节）完整解析")
    order = ">" if byte_order == "big" else "<"
    values = [item[0] for item in struct.iter_unpack(order + VALUE_FORMATS[value_type], payload)]
    try:
        scale = float(config.get("receive_scale", 1))
    except (TypeError, ValueError) as exc:
        raise DataCodecError("响应缩放系数无效") from exc
    if not math.isfinite(scale):
        raise DataCodecError("响应缩放系数必须是有限数值")
    if scale != 1:
        values = [value * scale for value in values]
    return values[0] if len(values) == 1 else values


def _validate_type_and_order(value_type: str, byte_order: str) -> None:
    if value_type not in VALUE_FORMATS:
        raise DataCodecError(f"不支持的数据类型：{value_type}")
    if byte_order not in {"big", "little"}:
        raise DataCodecError("字节序仅支持 big 或 little")


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise DataCodecError(f"{label}必须是非负整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise DataCodecError(f"{label}必须是非负整数") from exc
    if number < 0 or number != float(value):
        raise DataCodecError(f"{label}必须是非负整数")
    return number


def validate_codec_config(config: dict[str, Any]) -> None:
    send_format = str(config.get("send_format", "text"))
    receive_format = str(config.get("receive_format") or config.get("response_format", "text"))
    if send_format not in SEND_FORMATS:
        raise DataCodecError("下发格式仅支持 text、decimal 或 json")
    if receive_format not in RECEIVE_FORMATS:
        raise DataCodecError("响应格式仅支持 text、decimal、json 或 hex")
    if send_format == "decimal":
        _validate_type_and_order(str(config.get("send_type", "uint16")), str(config.get("send_byte_order", "big")))
        _scaled_numbers(0, config.get("send_scale", 1))
    if receive_format == "decimal":
        _validate_type_and_order(
            str(config.get("receive_type", "uint16")), str(config.get("receive_byte_order", "big"))
        )
        _nonnegative_int(config.get("receive_offset", 0), "响应偏移")
        _nonnegative_int(config.get("receive_length", 0), "响应截取长度")
        try:
            scale = float(config.get("receive_scale", 1))
        except (TypeError, ValueError) as exc:
            raise DataCodecError("响应缩放系数无效") from exc
        if not math.isfinite(scale):
            raise DataCodecError("响应缩放系数必须是有限数值")
