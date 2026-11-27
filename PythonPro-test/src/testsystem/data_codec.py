"""数据编解码：低代码页面的十进制输入 ⇄ 设备字节流之间的双向转换。

低代码页面统一用十进制/文本描述数据，协议层按通讯配置决定实际字节格式：
- 下发（encode_payload）：text/json 直接编码；decimal 按 send_type（uint16 等）、
  字节序、缩放系数打包为二进制；hex 为旧版兼容入口；
- 响应（decode_payload）：text/json 直接解析；decimal 按 receive_type/字节序/偏移/
  截取长度解包并缩放，单值返回标量、多值返回列表；hex 输出大写十六进制串。

所有配置错误抛 DataCodecError（ValueError 子类），保存协议配置时由 validate_codec_config 预检。
"""
from __future__ import annotations

import json
import math
import re
import struct
from decimal import Decimal, InvalidOperation
from typing import Any


class DataCodecError(ValueError):
    """编解码配置或数据错误（消息面向用户，可直接展示）。"""


# 十进制数值与 struct 打包格式的映射（整数/浮点、位宽、符号）
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
    """把输入解析为 Decimal 列表：支持标量、列表、以及逗号/空白分隔的字符串。

    用 Decimal 而不是 float 解析，避免二进制浮点误差在“×缩放系数”后被放大成错值。
    """
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
    """解析十进制输入并乘以缩放系数（如温度 25.6℃ × 10 = 256 下发）。"""
    try:
        factor = Decimal(str(scale))
    except InvalidOperation as exc:
        raise DataCodecError("数值缩放系数无效") from exc
    if not factor.is_finite():
        raise DataCodecError("数值缩放系数必须是有限数值")
    return [value * factor for value in _decimal_values(data)]


def encode_payload(data: Any, config: dict[str, Any], legacy_encoding: str | None = None) -> bytes:
    """按协议配置把动作数据编码为待发送字节；legacy_encoding 兼容 v1 流程动作里的 encoding 字段。"""
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
            # 整型打包前拒绝非整数（如缩放后仍是 25.5），防止 struct 静默截断
            if value != integer:
                raise DataCodecError(f"{value_type} 不能保存非整数：{value}")
            converted.append(integer)
    try:
        return struct.pack((">" if byte_order == "big" else "<") + VALUE_FORMATS[value_type] * len(converted), *converted)
    except (struct.error, OverflowError) as exc:
        raise DataCodecError(f"数值超出 {value_type} 可表示范围：{exc}") from exc


def decode_payload(data: bytes, config: dict[str, Any], legacy_format: str | None = None) -> Any:
    """把设备返回字节解析为断言可用的值；兼容旧字段名 response_format。

    decimal 模式支持偏移/截取（跳过帧头帧尾）与缩放还原；单值返回标量便于直接断言。
    """
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
    """校验数值类型与字节序的合法性。"""
    if value_type not in VALUE_FORMATS:
        raise DataCodecError(f"不支持的数据类型：{value_type}")
    if byte_order not in {"big", "little"}:
        raise DataCodecError("字节序仅支持 big 或 little")


def _nonnegative_int(value: Any, label: str) -> int:
    """非负整数校验（拒绝布尔与小数），label 用于错误消息定位字段。"""
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
    """保存协议配置前的预检：格式合法、数值类型/字节序/偏移/缩放可用（用样例值试算）。"""
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
