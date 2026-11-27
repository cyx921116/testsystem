"""协议目录：每种通讯协议的页面表单字段定义与默认配置。

通讯配置页按这里的 fields 渲染表单（分组：connection 连接参数 / downlink 下发编码 /
uplink 响应解析），``depends_on`` 实现字段联动（如选择“十进制”格式才显示数值类型）。
文件末尾把编解码字段统一追加到各协议，并汇总出 ``DEFAULT_PROTOCOL_CONFIGS``
供数据库初始化时写入 protocol_configs 表。
"""
from __future__ import annotations

from typing import Any


def field(
    key: str,
    label: str,
    kind: str = "text",
    default: Any = None,
    *,
    required: bool = False,
    options: list[dict[str, Any]] | None = None,
    placeholder: str = "",
    group: str = "connection",
    help: str = "",
    depends_on: dict[str, str] | None = None,
) -> dict[str, Any]:
    """构造一个表单字段描述：kind 支持 text/number/select/checkbox/password/json。"""
    return {
        "key": key,
        "label": label,
        "type": kind,
        "default": default,
        "required": required,
        "options": options or [],
        "placeholder": placeholder,
        "group": group,
        "help": help,
        "depends_on": depends_on or {},
    }


# 各协议的连接参数字段定义（编解码字段由 codec_fields 统一追加）
PROTOCOL_DEFINITIONS: dict[str, dict[str, Any]] = {
    "simulator": {
        "label": "内置仿真器",
        "description": "无需硬件即可验证扫码、测试、报告和 MES 全流程。",
        "fields": [
            field("default_response", "默认响应", default="OK"),
            field("loopback", "未匹配时回显发送内容", "checkbox", False),
            field("latency_ms", "仿真延迟（毫秒）", "number", 30),
            field("responses", "请求响应映射", "json", {}),
            field("values", "execute 操作响应映射", "json", {}),
        ],
    },
    "serial": {
        "label": "串口 Serial",
        "description": "通过 pyserial 连接 RS-232、USB 转串口等设备。",
        "fields": [
            field("port", "串口号", default="COM3", required=True, placeholder="COM3 或 /dev/ttyUSB0"),
            field("baudrate", "波特率", "number", 115200, required=True),
            field("bytesize", "数据位", "number", 8),
            field("parity", "校验位", "select", "N", options=[{"value": "N", "label": "无"}, {"value": "E", "label": "偶校验"}, {"value": "O", "label": "奇校验"}]),
            field("stopbits", "停止位", "number", 1),
            field("timeout_ms", "读取超时（毫秒）", "number", 1500),
            field("encoding", "字符编码", default="utf-8"),
        ],
    },
    "can": {
        "label": "CAN 总线",
        "description": "支持 python-can 已适配的 PCAN、SocketCAN、Kvaser 等接口；ID 和数据均按十进制编辑。",
        "fields": [
            field("interface", "驱动接口", default="pcan", required=True, placeholder="pcan / socketcan / kvaser"),
            field("channel", "通道", default="PCAN_USBBUS1", required=True),
            field("bitrate", "波特率", "number", 500000),
            field("arbitration_id", "发送仲裁 ID（十进制）", "number", 1793),
            field("response_id", "响应仲裁 ID（十进制，0 不过滤）", "number", 1801),
            field("extended_id", "扩展帧", "checkbox", False),
            field("can_fd", "CAN FD", "checkbox", False),
            field("timeout_ms", "读取超时（毫秒）", "number", 1500),
        ],
    },
    "tcp": {
        "label": "TCP",
        "description": "标准 TCP Client，支持文本指令和可配置的十进制二进制数据。",
        "fields": [
            field("host", "服务器地址", default="192.168.1.100", required=True),
            field("port", "端口", "number", 5025, required=True),
            field("timeout_ms", "通讯超时（毫秒）", "number", 1500),
            field("read_size", "接收字节数", "number", 4096),
            field("encoding", "字符编码", default="utf-8"),
        ],
    },
    "wifi": {
        "label": "WiFi（TCP）",
        "description": "通过已建立的 WiFi 网络使用 TCP 通讯；网络名称用于工位追溯。",
        "fields": [
            field("network_name", "网络名称（SSID）", default="PRODUCTION-NET"),
            field("host", "设备地址", default="192.168.1.100", required=True),
            field("port", "端口", "number", 8080, required=True),
            field("timeout_ms", "通讯超时（毫秒）", "number", 2000),
            field("read_size", "接收字节数", "number", 4096),
            field("encoding", "字符编码", default="utf-8"),
        ],
    },
    "modbus": {
        "label": "Modbus",
        "description": "支持 Modbus TCP/RTU，读写线圈、离散量和寄存器。",
        "fields": [
            field("mode", "工作模式", "select", "tcp", options=[{"value": "tcp", "label": "Modbus TCP"}, {"value": "rtu", "label": "Modbus RTU"}]),
            field("host", "TCP 地址", default="192.168.1.100"),
            field("port", "TCP 端口", "number", 502),
            field("serial_port", "RTU 串口", default="COM3"),
            field("baudrate", "RTU 波特率", "number", 9600),
            field("unit_id", "从站地址", "number", 1),
            field("timeout_ms", "通讯超时（毫秒）", "number", 1500),
        ],
    },
    "bluetooth": {
        "label": "蓝牙 BLE",
        "description": "通过 BLE GATT 特征值读写数据；经典蓝牙 SPP 请配置为系统虚拟串口。",
        "fields": [
            field("address", "设备 MAC / 标识", default="AA:BB:CC:DD:EE:FF", required=True),
            field("write_characteristic", "写入特征 UUID", default="0000ffe1-0000-1000-8000-00805f9b34fb", required=True),
            field("read_characteristic", "读取特征 UUID", default="0000ffe1-0000-1000-8000-00805f9b34fb", required=True),
            field("write_with_response", "写入后等待应答", "checkbox", True),
            field("timeout_ms", "通讯超时（毫秒）", "number", 5000),
            field("encoding", "字符编码", default="utf-8"),
        ],
    },
    "mqtt": {
        "label": "MQTT",
        "description": "向请求主题发布指令，并从响应主题等待产品应答。",
        "fields": [
            field("host", "Broker 地址", default="127.0.0.1", required=True),
            field("port", "端口", "number", 1883),
            field("client_id", "客户端 ID", default="testsystem-station-01"),
            field("username", "用户名", default=""),
            field("password", "密码", "password", ""),
            field("request_topic", "请求主题", default="factory/device/request", required=True),
            field("response_topic", "响应主题", default="factory/device/response", required=True),
            field("qos", "QoS", "number", 1),
            field("use_tls", "启用 TLS", "checkbox", False),
            field("timeout_ms", "响应超时（毫秒）", "number", 3000),
        ],
    },
    "jlink": {
        "label": "SEGGER J-Link",
        "description": "通过 J-Link 读写芯片内存和寄存器，需安装 J-Link Software。",
        "fields": [
            field("dll_path", "JLinkARM.dll 路径（可选）", default=""),
            field("chip_name", "芯片型号", default="Cortex-M4", required=True),
            field("speed_khz", "SWD/JTAG 速率（kHz）", "number", 4000),
            field("interface", "调试接口", "select", "SWD", options=[{"value": "SWD", "label": "SWD"}, {"value": "JTAG", "label": "JTAG"}]),
            field("timeout_ms", "操作超时（毫秒）", "number", 5000),
        ],
    },
}


# 十进制编解码共用的下拉选项
VALUE_TYPE_OPTIONS = [
    {"value": value, "label": label}
    for value, label in [
        ("uint8", "无符号 8 位整数"),
        ("int8", "有符号 8 位整数"),
        ("uint16", "无符号 16 位整数"),
        ("int16", "有符号 16 位整数"),
        ("uint32", "无符号 32 位整数"),
        ("int32", "有符号 32 位整数"),
        ("float32", "32 位浮点数"),
        ("float64", "64 位浮点数"),
    ]
]
BYTE_ORDER_OPTIONS = [
    {"value": "big", "label": "大端（高字节在前）"},
    {"value": "little", "label": "小端（低字节在前）"},
]


def codec_fields(send_format: str, receive_format: str) -> list[dict[str, Any]]:
    """生成下发编码（downlink）与响应解析（uplink）两组通用字段。

    send_format/receive_format 为该协议的默认格式；数值类型、字节序、缩放、偏移等
    字段通过 depends_on 挂在 decimal 选项上，仅当用户选择“十进制”时才显示。
    """
    decimal_send = {"key": "send_format", "value": "decimal"}
    decimal_receive = {"key": "receive_format", "value": "decimal"}
    return [
        field(
            "send_format", "下发数据格式", "select", send_format,
            options=[
                {"value": "text", "label": "文本指令"},
                {"value": "decimal", "label": "十进制数值 → 二进制"},
                {"value": "json", "label": "JSON 文本"},
            ],
            group="downlink",
        ),
        field("send_type", "数值类型", "select", "uint16", options=VALUE_TYPE_OPTIONS, group="downlink", depends_on=decimal_send),
        field("send_byte_order", "字节序", "select", "big", options=BYTE_ORDER_OPTIONS, group="downlink", depends_on=decimal_send),
        field(
            "send_scale", "下发缩放系数", "number", 1, group="downlink", depends_on=decimal_send,
            help="实际写入值 = 页面十进制输入 × 此系数；多个数值可用逗号分隔。",
        ),
        field(
            "receive_format", "响应解析格式", "select", receive_format,
            options=[
                {"value": "text", "label": "文本"},
                {"value": "decimal", "label": "二进制 → 十进制数值"},
                {"value": "json", "label": "JSON"},
                {"value": "hex", "label": "原始 HEX（诊断）"},
            ],
            group="uplink",
        ),
        field("receive_type", "数值类型", "select", "uint16", options=VALUE_TYPE_OPTIONS, group="uplink", depends_on=decimal_receive),
        field("receive_byte_order", "字节序", "select", "big", options=BYTE_ORDER_OPTIONS, group="uplink", depends_on=decimal_receive),
        field("receive_offset", "起始字节偏移", "number", 0, group="uplink", depends_on=decimal_receive),
        field("receive_length", "解析字节数（0 为全部）", "number", 0, group="uplink", depends_on=decimal_receive),
        field(
            "receive_scale", "响应缩放系数", "number", 1, group="uplink", depends_on=decimal_receive,
            help="页面和断言使用的数值 = 解析值 × 此系数，例如原始值 1200 可缩放为 12.0。",
        ),
    ]


# 给需要编解码的协议统一追加通用字段（CAN 默认十进制二进制帧，其余默认文本）；
# modbus/jlink 走 execute 操作语义，不经过通用编解码，故不追加
for protocol_name, send_default, receive_default in [
    ("serial", "text", "text"),
    ("can", "decimal", "decimal"),
    ("tcp", "text", "text"),
    ("wifi", "text", "text"),
    ("bluetooth", "text", "text"),
    ("mqtt", "text", "text"),
]:
    PROTOCOL_DEFINITIONS[protocol_name]["fields"].extend(codec_fields(send_default, receive_default))


# 各协议的默认连接配置（字段默认值汇总），数据库初始化时写入 protocol_configs 表
DEFAULT_PROTOCOL_CONFIGS = {
    name: {item["key"]: item["default"] for item in definition["fields"]}
    for name, definition in PROTOCOL_DEFINITIONS.items()
}
