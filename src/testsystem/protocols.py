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
    pass


def _as_int(value: Any) -> int:
    if isinstance(value, str):
        text = value.strip()
        return int(text, 16) if text.lower().startswith("0x") else int(text, 10)
    return int(value)


def _encode(data: Any, encoding: str, charset: str = "utf-8") -> bytes:
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
    if response_format == "hex":
        return data.hex(" ").upper()
    return data.decode(charset, errors="replace").strip()


class BaseProtocol:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def encode_data(self, data: Any, encoding: str | None = None) -> bytes:
        return encode_payload(data, self.config, encoding)

    def decode_data(self, data: bytes, response_format: str | None = None) -> Any:
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
        self.connect()
        return {"ok": True, "message": "连接成功"}


class SimulatorProtocol(BaseProtocol):
    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.context: dict[str, Any] = {}

    def set_context(self, context: dict[str, Any]) -> None:
        self.context = context

    def request(self, data: Any, encoding: str | None = None, timeout_ms: int = 1000) -> Any:
        self.connect()
        time.sleep(max(0, int(self.config.get("latency_ms", 30))) / 1000)
        text = str(data).strip()
        responses = self.config.get("responses", {})
        if text in responses:
            return responses[text]
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
    def _run(self, coroutine: Any) -> Any:
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
    try:
        return PROTOCOL_CLASSES[name](config)
    except KeyError as exc:
        raise ProtocolError(f"不支持的通讯协议：{name}") from exc


def protocol_catalog() -> list[dict[str, Any]]:
    catalog = []
    for name, definition in PROTOCOL_DEFINITIONS.items():
        module = DRIVER_MODULES[name]
        catalog.append({
            "name": name,
            **definition,
            "driver_available": module is None or importlib.util.find_spec(module) is not None,
        })
    return catalog
