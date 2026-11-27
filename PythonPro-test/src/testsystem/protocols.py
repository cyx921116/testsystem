"""通讯协议驱动层：统一的发送/接收/请求/设备操作接口与各协议实现。

执行器只面向 ``BaseProtocol`` 的四个动作语义编程：
- send     仅发送（编码由协议配置的 downlink 段决定）
- receive  等待接收（解析由 uplink 段决定）
- request  发送并等待响应（默认 send + receive，部分协议有原子实现）
- execute  设备操作（Modbus 读写寄存器、J-Link 读写内存等协议专属能力）

硬件驱动（pyserial/python-can/pymodbus/bleak/paho-mqtt/pylink）都是可选依赖，
未安装时对应协议在通讯配置页标记“驱动缺失”，调用时给出明确安装提示；
内置仿真器（SimulatorProtocol）无任何依赖，可完整演示测试流程。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
import threading
import time
from typing import Any

from .data_codec import decode_payload, encode_payload
from .protocol_definitions import PROTOCOL_DEFINITIONS


class ProtocolError(RuntimeError):
    """协议连接/收发/操作失败（消息面向用户，可直接展示在测试项与提示中）。"""


def _as_int(value: Any) -> int:
    """把配置值转为整数：字符串支持 0x 前缀十六进制，其余按十进制。"""
    if isinstance(value, str):
        text = value.strip()
        return int(text, 16) if text.lower().startswith("0x") else int(text, 10)
    return int(value)


def _encode(data: Any, encoding: str, charset: str = "utf-8") -> bytes:
    """旧版编码入口（text/hex/json），现由 data_codec.encode_payload 统一处理，此处保留兼容。"""
    if encoding == "hex":
        if not isinstance(data, str):
            raise ProtocolError("HEX 发送数据必须是字符串")
        try:
            return bytes.fromhex(data)
        except ValueError as exc:
            raise ProtocolError(f"HEX 数据格式错误：{exc}") from exc
    if encoding == "json":
        return json.dumps(data, ensure_ascii=False).encode(charset)
    return str(data).encode(charset)


def _decode(data: bytes, charset: str = "utf-8", response_format: str = "text") -> str:
    """旧版解码入口（text/hex），保留兼容；新逻辑见 data_codec.decode_payload。"""
    if response_format == "hex":
        return data.hex(" ").upper()
    return data.decode(charset, errors="replace").strip()


class BaseProtocol:
    """协议基类：持有连接配置，默认实现“连接占位 + request=send+receive”。

    子类按需覆盖 connect/send/receive/execute；不支持的动作抛出 ProtocolError，
    编译期无法知道协议能力，运行期以明确错误暴露配置问题。
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def encode_data(self, data: Any, encoding: str | None = None) -> bytes:
        """按协议配置编码下发数据（encoding 为 v1 动作里的旧版覆盖项）。"""
        return encode_payload(data, self.config, encoding)

    def decode_data(self, data: bytes, response_format: str | None = None) -> Any:
        """按协议配置解析响应字节为断言可用的值。"""
        return decode_payload(data, self.config, response_format)

    def send(self, data: Any, encoding: str | None = None) -> None:
        raise ProtocolError("当前协议不支持 send 动作")

    def receive(self, timeout_ms: int = 1000, size: int = 4096) -> Any:
        raise ProtocolError("当前协议不支持 receive 动作")

    def request(self, data: Any, encoding: str | None = None, timeout_ms: int = 1000) -> Any:
        self.send(data, encoding)
        return self.receive(timeout_ms=timeout_ms)

    def execute(self, operation: str, params: dict[str, Any]) -> Any:
        raise ProtocolError(f"当前协议不支持 execute/{operation}")

    def test_connection(self) -> dict[str, Any]:
        """通讯配置页的“测试连接”：默认仅验证能建立连接。"""
        self.connect()
        return {"ok": True, "message": "连接成功"}


class SimulatorProtocol(BaseProtocol):
    """内置仿真器：按“请求响应映射 → 内置 AT 指令 → 回显/默认响应”的顺序应答。

    可通过配置 latency_ms 模拟设备耗时、responses/values 定制应答，
    用于无硬件时验证扫码、执行、报告与 MES 全链路。
    """

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.context: dict[str, Any] = {}

    def set_context(self, context: dict[str, Any]) -> None:
        """注入运行上下文（SN 等），内置 AT+SN? 应答会返回当前产品 SN。"""
        self.context = context

    def request(self, data: Any, encoding: str | None = None, timeout_ms: int = 1000) -> Any:
        self.connect()
        time.sleep(max(0, int(self.config.get("latency_ms", 30))) / 1000)
        text = str(data).strip()
        responses = self.config.get("responses", {})
        if text in responses:
            return responses[text]
        # 内置应答与演示流程（DEFAULT_SCRIPT）一一对应
        builtins = {
            "AT+SN?": self.context.get("sn", "UNKNOWN"),
            "AT+VERSION?": "1.0.0",
            "AT+VOLTAGE?": "12.0",
            "AT+SELFTEST?": "PASS",
        }
        if text in builtins:
            return builtins[text]
        if self.config.get("loopback"):
            return data
        return self.config.get("default_response", "OK")

    def send(self, data: Any, encoding: str | None = None) -> None:
        self.connect()

    def receive(self, timeout_ms: int = 1000, size: int = 4096) -> Any:
        return self.config.get("default_response", "OK")

    def execute(self, operation: str, params: dict[str, Any]) -> Any:
        return self.config.get("values", {}).get(operation, "OK")


class SerialProtocol(BaseProtocol):
    """串口（pyserial）：RS-232 / USB 转串口设备。"""

    def connect(self) -> None:
        if self.connected:
            return
        try:
            import serial
        except ImportError as exc:
            raise ProtocolError("缺少 pyserial，请执行 uv sync --extra hardware") from exc
        try:
            self.client = serial.Serial(
                port=self.config["port"],
                baudrate=int(self.config.get("baudrate", 115200)),
                bytesize=int(self.config.get("bytesize", 8)),
                parity=self.config.get("parity", "N"),
                stopbits=float(self.config.get("stopbits", 1)),
                timeout=float(self.config.get("timeout_ms", 1500)) / 1000,
                write_timeout=float(self.config.get("timeout_ms", 1500)) / 1000,
            )
            self.connected = True
        except Exception as exc:
            raise ProtocolError(f"串口连接失败：{exc}") from exc

    def send(self, data: Any, encoding: str | None = None) -> None:
        self.connect()
        self.client.write(self.encode_data(data, encoding))
        self.client.flush()

    def receive(self, timeout_ms: int = 1000, size: int = 4096) -> Any:
        self.connect()
        self.client.timeout = timeout_ms / 1000
        return self.decode_data(self.client.read(size))

    def close(self) -> None:
        if self.connected:
            self.client.close()
        super().close()

    def test_connection(self) -> dict[str, Any]:
        self.connect()
        return {"ok": True, "message": f"串口 {self.config['port']} 已打开"}


class TcpProtocol(BaseProtocol):
    """TCP 客户端：文本指令或可配置的二进制数据；WiFi 协议复用本实现。"""

    def connect(self) -> None:
        if self.connected:
            return
        try:
            self.client = socket.create_connection(
                (self.config["host"], int(self.config["port"])),
                timeout=float(self.config.get("timeout_ms", 1500)) / 1000,
            )
            self.client.settimeout(float(self.config.get("timeout_ms", 1500)) / 1000)
            self.connected = True
        except OSError as exc:
            raise ProtocolError(f"TCP 连接失败：{exc}") from exc

    def send(self, data: Any, encoding: str | None = None) -> None:
        self.connect()
        self.client.sendall(self.encode_data(data, encoding))

    def receive(self, timeout_ms: int = 1000, size: int = 4096) -> Any:
        self.connect()
        self.client.settimeout(timeout_ms / 1000)
        try:
            data = self.client.recv(int(self.config.get("read_size", size)))
        except socket.timeout as exc:
            raise ProtocolError("TCP 接收超时") from exc
        if not data:
            raise ProtocolError("TCP 对端已关闭连接")
        return self.decode_data(data)

    def close(self) -> None:
        if self.connected:
            self.client.close()
        super().close()

    def test_connection(self) -> dict[str, Any]:
        self.connect()
        return {"ok": True, "message": f"已连接 {self.config['host']}:{self.config['port']}"}


class CanProtocol(BaseProtocol):
    """CAN 总线（python-can）：按仲裁 ID 收发帧，可配置响应 ID 过滤。"""

    def connect(self) -> None:
        if self.connected:
            return
        try:
            import can
        except ImportError as exc:
            raise ProtocolError("缺少 python-can，请执行 uv sync --extra hardware") from exc
        try:
            self.can = can
            self.bus = can.Bus(
                interface=self.config.get("interface", "pcan"),
                channel=self.config["channel"],
                bitrate=int(self.config.get("bitrate", 500000)),
            )
            self.connected = True
        except Exception as exc:
            raise ProtocolError(f"CAN 连接失败：{exc}") from exc

    def send(self, data: Any, encoding: str | None = None) -> None:
        self.connect()
        message = self.can.Message(
            arbitration_id=_as_int(self.config.get("arbitration_id", "0x701")),
            is_extended_id=bool(self.config.get("extended_id", False)),
            is_fd=bool(self.config.get("can_fd", False)),
            data=self.encode_data(data, encoding),
        )
        self.bus.send(message, timeout=float(self.config.get("timeout_ms", 1500)) / 1000)

    def receive(self, timeout_ms: int = 1000, size: int = 4096) -> Any:
        """接收一帧：配置了 response_id 时循环丢弃不匹配的帧直到超时。"""
        self.connect()
        response_id = self.config.get("response_id")
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError("CAN 接收超时")
            message = self.bus.recv(timeout=remaining)
            if message is None:
                raise ProtocolError("CAN 接收超时")
            if response_id and message.arbitration_id != _as_int(response_id):
                continue
            return self.decode_data(message.data)

    def close(self) -> None:
        if self.connected:
            self.bus.shutdown()
        super().close()


class ModbusProtocol(BaseProtocol):
    """Modbus（pymodbus）：TCP/RTU 两种模式，execute 提供寄存器/线圈读写。"""

    def connect(self) -> None:
        if self.connected:
            return
        try:
            from pymodbus.client import ModbusSerialClient, ModbusTcpClient
        except ImportError as exc:
            raise ProtocolError("缺少 pymodbus，请执行 uv sync --extra hardware") from exc
        timeout = float(self.config.get("timeout_ms", 1500)) / 1000
        if self.config.get("mode", "tcp") == "rtu":
            self.client = ModbusSerialClient(
                port=self.config.get("serial_port", "COM3"),
                baudrate=int(self.config.get("baudrate", 9600)),
                timeout=timeout,
            )
        else:
            self.client = ModbusTcpClient(
                host=self.config.get("host", "127.0.0.1"),
                port=int(self.config.get("port", 502)),
                timeout=timeout,
            )
        if not self.client.connect():
            raise ProtocolError("Modbus 设备连接失败")
        self.connected = True

    def execute(self, operation: str, params: dict[str, Any]) -> Any:
        """执行 Modbus 操作：读保持/输入寄存器、读线圈/离散量、写单寄存器/线圈。"""
        self.connect()
        unit = int(self.config.get("unit_id", 1))
        address = _as_int(params.get("address", 0))
        count = int(params.get("count", 1))
        readers = {
            "read_holding_registers": self.client.read_holding_registers,
            "read_input_registers": self.client.read_input_registers,
            "read_coils": self.client.read_coils,
            "read_discrete_inputs": self.client.read_discrete_inputs,
        }
        try:
            if operation in readers:
                result = readers[operation](address=address, count=count, slave=unit)
            elif operation == "write_register":
                result = self.client.write_register(address=address, value=_as_int(params.get("value", 0)), slave=unit)
            elif operation == "write_coil":
                result = self.client.write_coil(address=address, value=bool(params.get("value")), slave=unit)
            else:
                raise ProtocolError(f"不支持的 Modbus 操作：{operation}")
        except ProtocolError:
            raise
        except Exception as exc:
            raise ProtocolError(f"Modbus 操作失败：{exc}") from exc
        if result.isError():
            raise ProtocolError(f"Modbus 返回错误：{result}")
        if hasattr(result, "registers"):
            return result.registers
        if hasattr(result, "bits"):
            return result.bits[:count]
        return "OK"

    def close(self) -> None:
        if self.connected:
            self.client.close()
        super().close()


class BluetoothProtocol(BaseProtocol):
    """BLE GATT（bleak）：写特征值后读特征值；asyncio 协程在线程池中同步执行。"""

    def _run(self, coroutine: Any) -> Any:
        """同步执行一段 BLE 协程，并把底层异常统一包装为 ProtocolError。"""
        try:
            return asyncio.run(coroutine)
        except Exception as exc:
            if isinstance(exc, ProtocolError):
                raise
            raise ProtocolError(f"BLE 通讯失败：{exc}") from exc

    async def _request(self, data: Any, encoding: str | None, read: bool) -> Any:
        try:
            from bleak import BleakClient
        except ImportError as exc:
            raise ProtocolError("缺少 bleak，请执行 uv sync --extra hardware") from exc
        timeout = float(self.config.get("timeout_ms", 5000)) / 1000
        async with BleakClient(self.config["address"], timeout=timeout) as client:
            if not client.is_connected:
                raise ProtocolError("BLE 设备未连接")
            await client.write_gatt_char(
                self.config["write_characteristic"],
                self.encode_data(data, encoding),
                response=bool(self.config.get("write_with_response", True)),
            )
            if read:
                value = await client.read_gatt_char(self.config["read_characteristic"])
                return self.decode_data(value)
        return None

    def request(self, data: Any, encoding: str | None = None, timeout_ms: int = 1000) -> Any:
        return self._run(self._request(data, encoding, True))

    def send(self, data: Any, encoding: str | None = None) -> None:
        self._run(self._request(data, encoding, False))

    def receive(self, timeout_ms: int = 1000, size: int = 4096) -> Any:
        async def read() -> str:
            try:
                from bleak import BleakClient
            except ImportError as exc:
                raise ProtocolError("缺少 bleak，请执行 uv sync --extra hardware") from exc
            async with BleakClient(self.config["address"], timeout=timeout_ms / 1000) as client:
                value = await client.read_gatt_char(self.config["read_characteristic"])
                return self.decode_data(value)

        return self._run(read())

    def test_connection(self) -> dict[str, Any]:
        async def connect() -> bool:
            try:
                from bleak import BleakClient
            except ImportError as exc:
                raise ProtocolError("缺少 bleak，请执行 uv sync --extra hardware") from exc
            async with BleakClient(
                self.config["address"], timeout=float(self.config.get("timeout_ms", 5000)) / 1000
            ) as client:
                return client.is_connected

        if not self._run(connect()):
            raise ProtocolError("BLE 设备未连接")
        return {"ok": True, "message": "BLE GATT 连接成功"}


class MqttProtocol(BaseProtocol):
    """MQTT（paho-mqtt）：向请求主题发布、在响应主题等待；后台 loop 线程接收消息。"""

    def connect(self) -> None:
        if self.connected:
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise ProtocolError("缺少 paho-mqtt，请执行 uv sync --extra hardware") from exc
        self.response_event = threading.Event()
        self.response_lock = threading.Lock()
        self.last_response: bytes | None = None
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.config.get("client_id", ""))
        if self.config.get("username"):
            self.client.username_pw_set(self.config["username"], self.config.get("password", ""))
        if self.config.get("use_tls"):
            self.client.tls_set()

        def on_message(client: Any, userdata: Any, message: Any) -> None:
            # 响应主题收到消息：记录负载并唤醒等待中的 receive/request
            with self.response_lock:
                self.last_response = bytes(message.payload)
            self.response_event.set()

        self.client.on_message = on_message
        try:
            self.client.connect(
                self.config["host"],
                int(self.config.get("port", 1883)),
                keepalive=30,
            )
            self.client.loop_start()
            self.client.subscribe(self.config["response_topic"], qos=int(self.config.get("qos", 1)))
            self.connected = True
        except Exception as exc:
            raise ProtocolError(f"MQTT 连接失败：{exc}") from exc

    def send(self, data: Any, encoding: str | None = None) -> None:
        self.connect()
        result = self.client.publish(
            self.config["request_topic"], self.encode_data(data, encoding), qos=int(self.config.get("qos", 1))
        )
        result.wait_for_publish(timeout=float(self.config.get("timeout_ms", 3000)) / 1000)

    def receive(self, timeout_ms: int = 1000, size: int = 4096) -> Any:
        self.connect()
        self.response_event.clear()
        if not self.response_event.wait(timeout_ms / 1000):
            raise ProtocolError("MQTT 响应超时")
        with self.response_lock:
            return self.decode_data(self.last_response or b"")

    def request(self, data: Any, encoding: str | None = None, timeout_ms: int = 1000) -> Any:
        """先清事件再发送，避免把上一次遗留的响应误当本次应答。"""
        self.connect()
        self.response_event.clear()
        self.send(data, encoding)
        if not self.response_event.wait(timeout_ms / 1000):
            raise ProtocolError("MQTT 响应超时")
        with self.response_lock:
            return self.decode_data(self.last_response or b"")

    def close(self) -> None:
        if self.connected:
            self.client.loop_stop()
            self.client.disconnect()
        super().close()


class JLinkProtocol(BaseProtocol):
    """SEGGER J-Link（pylink-square）：内存/寄存器读写与复位控制，常用于烧录后校验。"""

    def connect(self) -> None:
        if self.connected:
            return
        try:
            import pylink
        except ImportError as exc:
            raise ProtocolError("缺少 pylink-square，请执行 uv sync --extra hardware") from exc
        try:
            self.pylink = pylink
            self.device = pylink.JLink()
            self.device.open(self.config.get("dll_path") or None)
            interface = self.config.get("interface", "SWD")
            self.device.set_tif(pylink.enums.JLinkInterfaces[interface])
            self.device.connect(
                self.config.get("chip_name", "Cortex-M4"),
                speed=int(self.config.get("speed_khz", 4000)),
            )
            self.connected = True
        except Exception as exc:
            raise ProtocolError(f"J-Link 连接失败：{exc}") from exc

    def execute(self, operation: str, params: dict[str, Any]) -> Any:
        """执行 J-Link 操作：read/write_memory、read/write_register、reset/halt/go。"""
        self.connect()
        try:
            if operation == "read_memory":
                values = self.device.memory_read(_as_int(params["address"]), int(params.get("size", 4)))
                return bytes(values).hex(" ").upper()
            if operation == "write_memory":
                values = params.get("data", [])
                if isinstance(values, str):
                    values = list(bytes.fromhex(values))
                self.device.memory_write(_as_int(params["address"]), values)
                return "OK"
            if operation == "read_register":
                return self.device.register_read(int(params["register"]))
            if operation == "write_register":
                self.device.register_write(int(params["register"]), _as_int(params["value"]))
                return "OK"
            if operation in {"reset", "halt", "go"}:
                getattr(self.device, operation)()
                return "OK"
            raise ProtocolError(f"不支持的 J-Link 操作：{operation}")
        except ProtocolError:
            raise
        except Exception as exc:
            raise ProtocolError(f"J-Link 操作失败：{exc}") from exc

    def close(self) -> None:
        if self.connected:
            self.device.close()
        super().close()

    def test_connection(self) -> dict[str, Any]:
        self.connect()
        return {"ok": True, "message": f"J-Link 已连接，Core ID: {self.device.core_id()}"}


# 协议名 → 驱动类（wifi 复用 TCP 实现）
PROTOCOL_CLASSES = {
    "simulator": SimulatorProtocol,
    "serial": SerialProtocol,
    "can": CanProtocol,
    "tcp": TcpProtocol,
    "wifi": TcpProtocol,
    "modbus": ModbusProtocol,
    "bluetooth": BluetoothProtocol,
    "mqtt": MqttProtocol,
    "jlink": JLinkProtocol,
}

# 协议名 → 依赖的驱动模块（None 表示无外部依赖），用于“驱动可用”状态展示
DRIVER_MODULES = {
    "simulator": None,
    "serial": "serial",
    "can": "can",
    "tcp": None,
    "wifi": None,
    "modbus": "pymodbus",
    "bluetooth": "bleak",
    "mqtt": "paho",
    "jlink": "pylink",
}


def create_protocol(name: str, config: dict[str, Any]) -> BaseProtocol:
    """按协议名实例化驱动；未知协议抛 ProtocolError。"""
    try:
        return PROTOCOL_CLASSES[name](config)
    except KeyError as exc:
        raise ProtocolError(f"不支持的通讯协议：{name}") from exc


def protocol_catalog() -> list[dict[str, Any]]:
    """通讯配置页的目录数据：字段定义 + 驱动是否可用（按依赖模块能否导入判断）。"""
    catalog = []
    for name, definition in PROTOCOL_DEFINITIONS.items():
        module = DRIVER_MODULES[name]
        catalog.append({
            "name": name,
            **definition,
            "driver_available": module is None or importlib.util.find_spec(module) is not None,
        })
    return catalog
