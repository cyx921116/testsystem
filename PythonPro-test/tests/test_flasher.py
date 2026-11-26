from __future__ import annotations

import sys

import pytest

from testsystem.flasher import (
    FlashError,
    SimulatorFlasher,
    create_flasher,
    find_firmware,
    flasher_catalog,
    load_firmware_index,
    register_firmware,
    sha256_file,
    validate_flash_request,
)


@pytest.fixture()
def firmware_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTSYSTEM_DATA_DIR", str(tmp_path))
    source = tmp_path / "app.bin"
    source.write_bytes(b"\x01\x02\x03\x04" * 64)
    return source


def test_register_and_find_firmware(firmware_repo):
    entry = register_firmware(firmware_repo, version="1.2.0", product_model="DEMO-100")
    assert entry["name"] == "app.bin"
    assert entry["version"] == "1.2.0"
    assert entry["size"] == 256
    assert entry["sha256"] == sha256_file(firmware_repo)
    assert len(load_firmware_index()) == 1

    by_name = find_firmware("app.bin")
    by_id = find_firmware(entry["id"])
    assert by_name["id"] == by_id["id"] == entry["id"]
    assert by_name["path"]


def test_register_same_name_replaces_index_entry(firmware_repo):
    register_firmware(firmware_repo, version="1.0.0")
    register_firmware(firmware_repo, version="1.1.0")
    index = load_firmware_index()
    assert len(index) == 1
    assert index[0]["version"] == "1.1.0"


def test_find_firmware_rejects_tampered_file(firmware_repo):
    register_firmware(firmware_repo, version="1.0.0")
    tampered = firmware_repo.with_name("firmware") / "app.bin"
    tampered.write_bytes(b"broken")
    with pytest.raises(FlashError, match="校验和不匹配"):
        find_firmware("app.bin")


def test_find_firmware_unknown_name(firmware_repo):
    with pytest.raises(FlashError, match="固件未登记"):
        find_firmware("missing.bin")


def test_register_rejects_missing_file_and_blank_version(firmware_repo):
    with pytest.raises(FlashError, match="固件文件不存在"):
        register_firmware(firmware_repo.with_name("none.bin"), version="1.0.0")
    with pytest.raises(FlashError, match="固件版本不能为空"):
        register_firmware(firmware_repo, version=" ")


def test_validate_flash_request_merges_defaults(firmware_repo):
    register_firmware(firmware_repo, version="1.0.0")
    request = validate_flash_request({"firmware": "app.bin", "sn": "SN-1"})
    assert request["firmware"] == "app.bin"
    assert request["options"] == {"erase": True, "verify": True, "reset": True, "timeout_ms": 60000}
    assert request["variables"] == {}


def test_validate_flash_request_rejects_bad_input(firmware_repo):
    with pytest.raises(FlashError, match="缺少固件"):
        validate_flash_request({"sn": "SN-1"})
    with pytest.raises(FlashError, match="烧录超时必须是数字"):
        validate_flash_request({"firmware": "app.bin", "options": {"timeout_ms": "abc"}})
    with pytest.raises(FlashError, match="烧录超时必须大于 0"):
        validate_flash_request({"firmware": "app.bin", "options": {"timeout_ms": 0}})


def test_simulator_flasher_success_with_progress(firmware_repo):
    register_firmware(firmware_repo, version="2.0.0")
    events: list[tuple[int, str]] = []
    flasher = SimulatorFlasher({"step_delay_ms": 0})
    result = flasher.flash(
        {"firmware": "app.bin", "sn": "SN-9"},
        on_progress=lambda percent, message: events.append((percent, message)),
    )
    assert result["ok"] is True
    assert result["backend"] == "simulator"
    assert result["firmware"]["version"] == "2.0.0"
    assert result["duration_ms"] >= 0
    assert [stage for _, stage in events] == ["连接烧录器", "擦除 Flash", "写入固件", "校验", "复位运行"]
    assert events[-1][0] == 100
    assert events == sorted(events, key=lambda item: item[0])


def test_simulator_flasher_respects_disabled_options(firmware_repo):
    register_firmware(firmware_repo, version="1.0.0")
    events: list[str] = []
    flasher = SimulatorFlasher({"step_delay_ms": 0})
    flasher.flash(
        {"firmware": "app.bin", "options": {"erase": False, "verify": False, "reset": False}},
        on_progress=lambda percent, message: events.append(message),
    )
    assert events == ["连接烧录器", "写入固件"]


def test_simulator_flasher_injected_failure(firmware_repo):
    register_firmware(firmware_repo, version="1.0.0")
    flasher = SimulatorFlasher({"step_delay_ms": 0, "fail_at_stage": "校验"})
    with pytest.raises(FlashError, match="仿真烧录在阶段“校验”失败"):
        flasher.flash({"firmware": "app.bin"})


def test_external_tool_flasher_success(firmware_repo):
    register_firmware(firmware_repo, version="1.0.0")
    flasher = create_flasher("external_tool", {
        "command": f'"{sys.executable}" -c "print(\'FLASH_DONE\')"',
        "success_text": "FLASH_DONE",
    })
    result = flasher.flash({"firmware": "app.bin", "sn": "SN-2"})
    assert result["ok"] is True
    assert result["details"]["returncode"] == 0
    assert "FLASH_DONE" in result["details"]["output_tail"]


def test_external_tool_flasher_failure(firmware_repo):
    register_firmware(firmware_repo, version="1.0.0")
    flasher = create_flasher("external_tool", {
        "command": f'"{sys.executable}" -c "import sys; sys.exit(3)"',
    })
    with pytest.raises(FlashError, match="返回码 3"):
        flasher.flash({"firmware": "app.bin"})


def test_external_tool_flasher_timeout(firmware_repo):
    register_firmware(firmware_repo, version="1.0.0")
    flasher = create_flasher("external_tool", {
        "command": f'"{sys.executable}" -c "import time; time.sleep(10)"',
        "timeout_ms": 300,
    })
    with pytest.raises(FlashError, match="超时"):
        flasher.flash({"firmware": "app.bin"})


def test_external_tool_requires_command(firmware_repo):
    register_firmware(firmware_repo, version="1.0.0")
    flasher = create_flasher("external_tool", {})
    assert flasher.probe()["ok"] is False
    with pytest.raises(FlashError, match="未配置烧录命令模板"):
        flasher.flash({"firmware": "app.bin"})


def test_create_flasher_unknown_backend():
    with pytest.raises(FlashError, match="不支持的烧录后端"):
        create_flasher("stlink")


def test_flasher_catalog_lists_backends():
    catalog = flasher_catalog()
    names = {item["name"] for item in catalog}
    assert names == {"simulator", "jlink", "external_tool"}
    simulator = next(item for item in catalog if item["name"] == "simulator")
    assert simulator["driver_available"] is True
    assert simulator["description"]
