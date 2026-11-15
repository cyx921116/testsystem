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
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "type": kind,
        "default": default,
        "required": required,
        "options": options or [],
        "placeholder": placeholder,
    }


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
        "description": "支持 python-can 已适配的 PCAN、SocketCAN、Kvaser 等接口。",
        "fields": [
            field("interface", "驱动接口", default="pcan", required=True, placeholder="pcan / socketcan / kvaser"),
            field("channel", "通道", default="PCAN_USBBUS1", required=True),
            field("bitrate", "波特率", "number", 500000),
            field("arbitration_id", "发送仲裁 ID", default="0x701"),
            field("response_id", "响应仲裁 ID（留空不过滤）", default="0x709"),
            field("extended_id", "扩展帧", "checkbox", False),
            field("can_fd", "CAN FD", "checkbox", False),
            field("timeout_ms", "读取超时（毫秒）", "number", 1500),
            field("response_format", "响应格式", "select", "hex", options=[{"value": "hex", "label": "HEX"}, {"value": "text", "label": "文本"}]),
        ],
    },
    "tcp": {
        "label": "TCP",
        "description": "标准 TCP Client，支持文本、HEX 和 JSON 数据。",
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


DEFAULT_PROTOCOL_CONFIGS = {
    name: {item["key"]: item["default"] for item in definition["fields"]}
    for name, definition in PROTOCOL_DEFINITIONS.items()
}
