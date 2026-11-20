from __future__ import annotations

import json
import socket
import threading

import pytest

from testsystem.data_codec import DataCodecError, decode_payload, encode_payload, validate_codec_config
from testsystem.database import Database
from testsystem.protocols import BaseProtocol, TcpProtocol


@pytest.mark.parametrize(
    ("value_type", "byte_order", "expected"),
    [
        ("uint16", "big", b"\x12\x34"),
        ("uint16", "little", b"\x34\x12"),
        ("int16", "big", b"\x12\x34"),
        ("uint32", "big", b"\x00\x00\x12\x34"),
    ],
)
def test_decimal_encoding_uses_configured_type_and_byte_order(
    value_type: str, byte_order: str, expected: bytes
) -> None:
    config = {"send_format": "decimal", "send_type": value_type, "send_byte_order": byte_order}
    assert encode_payload("4660", config) == expected


def test_decimal_encoding_supports_lists_and_scaling() -> None:
    config = {
        "send_format": "decimal",
        "send_type": "uint16",
        "send_byte_order": "big",
        "send_scale": 0.01,
    }
    assert encode_payload("1200, 2500", config) == b"\x00\x0c\x00\x19"


def test_decimal_decoding_supports_offset_length_and_scaling() -> None:
    config = {
        "receive_format": "decimal",
        "receive_type": "uint16",
        "receive_byte_order": "big",
        "receive_offset": 1,
        "receive_length": 4,
        "receive_scale": 0.01,
    }
    assert decode_payload(b"\xff\x04\xb0\x09\xc4\x00", config) == [12.0, 25.0]


def test_decimal_decoding_returns_scalar_for_single_value() -> None:
    config = {"receive_format": "decimal", "receive_type": "int16", "receive_byte_order": "big"}
    assert decode_payload(b"\xff\x9c", config) == -100


def test_non_decimal_input_is_rejected() -> None:
    config = {"send_format": "decimal", "send_type": "uint16", "send_byte_order": "big"}
    with pytest.raises(DataCodecError, match="仅支持十进制数值"):
        encode_payload("0x701", config)


def test_protocol_request_uses_shared_codec() -> None:
    class LoopbackProtocol(BaseProtocol):
        def __init__(self, config: dict):
            super().__init__(config)
            self.written = b""

        def send(self, data, encoding=None) -> None:
            self.written = self.encode_data(data, encoding)

        def receive(self, timeout_ms: int = 1000, size: int = 4096):
            return self.decode_data(self.written)

    protocol = LoopbackProtocol({
        "send_format": "decimal",
        "send_type": "uint16",
        "send_byte_order": "little",
        "receive_format": "decimal",
        "receive_type": "uint16",
        "receive_byte_order": "little",
    })
    assert protocol.request(1200) == 1200
    assert protocol.written == b"\xb0\x04"


def test_legacy_hex_action_remains_compatible() -> None:
    assert encode_payload("01 03 00 00", {}, "hex") == b"\x01\x03\x00\x00"


def test_invalid_codec_configuration_is_rejected() -> None:
    with pytest.raises(DataCodecError, match="响应偏移"):
        validate_codec_config({
            "receive_format": "decimal",
            "receive_type": "uint16",
            "receive_byte_order": "big",
            "receive_offset": -1,
        })


def test_legacy_protocol_config_is_preserved_and_extended(tmp_path) -> None:
    database = Database(tmp_path / "migration.db")
    database.initialize()
    legacy = {"arbitration_id": "0x701", "response_format": "hex", "timeout_ms": 2500}
    database.execute("UPDATE protocol_configs SET config_json = ? WHERE name = 'can'", (json.dumps(legacy),))

    database.initialize()
    migrated = json.loads(database.one("SELECT config_json FROM protocol_configs WHERE name = 'can'")["config_json"])
    assert migrated["arbitration_id"] == "0x701"
    assert migrated["timeout_ms"] == 2500
    assert migrated["send_format"] == "decimal"
    assert migrated["receive_format"] == "hex"


def test_tcp_protocol_applies_decimal_codec_to_real_socket() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(3)
    port = server.getsockname()[1]
    received: list[bytes] = []

    def respond() -> None:
        connection, _ = server.accept()
        with connection:
            received.append(connection.recv(2))
            connection.sendall(b"\x04\xb0")
        server.close()

    thread = threading.Thread(target=respond)
    thread.start()
    protocol = TcpProtocol({
        "host": "127.0.0.1",
        "port": port,
        "timeout_ms": 1000,
        "read_size": 2,
        "send_format": "decimal",
        "send_type": "uint16",
        "send_byte_order": "big",
        "receive_format": "decimal",
        "receive_type": "uint16",
        "receive_byte_order": "big",
        "receive_scale": 0.01,
    })
    try:
        result = protocol.request(1200, timeout_ms=1000)
    finally:
        protocol.close()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert received == [b"\x04\xb0"]
    assert result == 12.0
