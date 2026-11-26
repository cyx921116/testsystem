from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import testsystem.app as app_module
from testsystem.compiler import ScriptValidationError, compile_script
from testsystem.database import Database
from testsystem.flasher import register_firmware


@pytest.fixture()
def flash_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTSYSTEM_DATA_DIR", str(tmp_path / "data"))
    database = Database(tmp_path / "flash-test.db")
    database.initialize()
    monkeypatch.setattr(app_module, "database", database)
    monkeypatch.setattr(app_module.executor, "db", database)
    monkeypatch.setattr(app_module.auth_service, "db", database)
    source = tmp_path / "app_v1.bin"
    source.write_bytes(b"\x01\x02\x03\x04" * 64)
    return source


def login(client: TestClient, username: str, password: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text


def flash_script(firmware: str) -> dict:
    return {
        "schema_version": 1,
        "name": "烧录测试流程",
        "product_model": "FLASH-100",
        "variables": {},
        "steps": [
            {
                "id": "flash_step",
                "name": "固件烧录",
                "protocol": "simulator",
                "timeout_ms": 30000,
                "retries": 0,
                "on_fail": "stop",
                "actions": [
                    {
                        "type": "flash",
                        "firmware": firmware,
                        "save_as": "flash_result",
                        "options": {"erase": True, "verify": True, "reset": True, "timeout_ms": 60000},
                    },
                    {
                        "type": "assert",
                        "actual": "${flash_result.firmware.version}",
                        "operator": "equals",
                        "expected": "1.0.0",
                    },
                ],
            }
        ],
    }


def run_to_completion(client: TestClient, script_id: int, sn: str) -> dict:
    scan = client.post("/api/test-runs/scan", json={"sn": sn, "script_id": script_id})
    assert scan.status_code == 200, scan.text
    assert scan.json()["can_start"] is True
    started = client.post("/api/test-runs", json={"sn": sn, "script_id": script_id})
    assert started.status_code == 201, started.text
    run = started.json()
    for _ in range(200):
        run = client.get(f"/api/test-runs/{run['id']}").json()
        if run["status"] != "RUNNING":
            break
        time.sleep(0.05)
    return run


def test_compile_flash_action_checks_registration(flash_env) -> None:
    register_firmware(flash_env, version="1.0.0")
    compiled = compile_script(flash_script("app_v1.bin"))
    assert compiled["steps"][0]["actions"][0]["type"] == "flash"

    templated = compile_script(flash_script("${fw_name}"))
    assert templated["steps"][0]["actions"][0]["firmware"] == "${fw_name}"

    with pytest.raises(ScriptValidationError, match="固件未登记"):
        compile_script(flash_script("missing.bin"))

    broken = flash_script("app_v1.bin")
    del broken["steps"][0]["actions"][0]["firmware"]
    with pytest.raises(ScriptValidationError, match="firmware 不能为空"):
        compile_script(broken)

    bad_options = flash_script("app_v1.bin")
    bad_options["steps"][0]["actions"][0]["options"] = {"erase": "yes", "timeout_ms": 0}
    with pytest.raises(ScriptValidationError) as excinfo:
        compile_script(bad_options)
    assert "options.erase 必须是布尔值" in str(excinfo.value)
    assert "options.timeout_ms" in str(excinfo.value)


def test_compile_flash_action_in_control_flow(flash_env) -> None:
    register_firmware(flash_env, version="1.0.0")
    source = {
        "schema_version": 2,
        "name": "控制流烧录",
        "product_model": "FLASH-100",
        "variables": {},
        "steps": [
            {
                "type": "test",
                "id": "flash_node",
                "name": "烧录节点",
                "protocol": "simulator",
                "on_fail": "stop",
                "actions": [{"type": "flash", "firmware": "app_v1.bin"}],
            }
        ],
        "subflows": {},
    }
    compiled = compile_script(source)
    assert compiled["schema_version"] == 2
    assert compiled["steps"][0]["actions"][0]["type"] == "flash"


def test_firmware_and_flasher_api(flash_env) -> None:
    admin = TestClient(app_module.app)
    login(admin, "admin", "admin123")

    created = admin.post("/api/firmware", json={"source": str(flash_env), "version": "1.0.0", "product_model": "FLASH-100"})
    assert created.status_code == 201, created.text
    entry = created.json()
    assert entry["name"] == "app_v1.bin"
    assert entry["sha256"]

    listing = admin.get("/api/firmware")
    assert listing.status_code == 200
    assert [item["name"] for item in listing.json()] == ["app_v1.bin"]

    verified = admin.post("/api/firmware/app_v1.bin/verify")
    assert verified.status_code == 200
    assert verified.json()["ok"] is True

    flash_env.with_name("data").joinpath("firmware", "app_v1.bin").write_bytes(b"tampered")
    tampered = admin.post("/api/firmware/app_v1.bin/verify")
    assert tampered.status_code == 422
    assert "校验和不匹配" in tampered.text

    catalog = admin.get("/api/flashers")
    assert catalog.status_code == 200
    assert {item["name"] for item in catalog.json()} == {"simulator", "jlink", "external_tool"}

    config = admin.get("/api/flasher/config")
    assert config.status_code == 200
    assert config.json()["backend"] == "simulator"
    saved = admin.put("/api/flasher/config", json={"backend": "simulator", "config": {"step_delay_ms": 0}})
    assert saved.status_code == 200
    assert admin.put("/api/flasher/config", json={"backend": "stlink", "config": {}}).status_code == 422
    probed = admin.post("/api/flasher/test", json={"backend": "simulator", "config": {}})
    assert probed.status_code == 200
    assert probed.json()["ok"] is True

    operator = TestClient(app_module.app)
    login(operator, "operator", "operator123")
    assert operator.get("/api/firmware").status_code == 403
    assert operator.post("/api/firmware", json={"source": "x", "version": "1"}).status_code == 403
    assert operator.get("/api/flasher/config").status_code == 403


def test_flash_run_end_to_end(flash_env) -> None:
    admin = TestClient(app_module.app)
    login(admin, "admin", "admin123")
    assert admin.post("/api/firmware", json={"source": str(flash_env), "version": "1.0.0"}).status_code == 201
    assert admin.put("/api/flasher/config", json={"backend": "simulator", "config": {"step_delay_ms": 0}}).status_code == 200
    created = admin.post("/api/scripts", json={"definition": flash_script("app_v1.bin")})
    assert created.status_code == 201, created.text
    script_id = created.json()["id"]

    run = run_to_completion(admin, script_id, "SN-FLASH-0001")
    assert run["status"] == "PASS", run
    assert all(item["status"] == "PASS" for item in run["items"])
    assert len(run["flash_records"]) == 1
    record = run["flash_records"][0]
    assert record["ok"] is True
    assert record["backend"] == "simulator"
    assert record["firmware_name"] == "app_v1.bin"
    assert record["firmware_version"] == "1.0.0"
    assert record["sn"] == "SN-FLASH-0001"

    report = admin.get(f"/api/reports/{run['report_id']}").json()
    assert report["status"] == "PASS"
    assert len(report["flash_records"]) == 1
    assert report["flash_records"][0]["firmware"]["sha256"]
    assert report["flash_records"][0]["item_path"] == "flash_step"


def test_flash_failure_marks_run_failed(flash_env) -> None:
    admin = TestClient(app_module.app)
    login(admin, "admin", "admin123")
    assert admin.post("/api/firmware", json={"source": str(flash_env), "version": "1.0.0"}).status_code == 201
    failed_config = {"backend": "simulator", "config": {"step_delay_ms": 0, "fail_at_stage": "校验"}}
    assert admin.put("/api/flasher/config", json=failed_config).status_code == 200
    created = admin.post("/api/scripts", json={"definition": flash_script("app_v1.bin")})
    assert created.status_code == 201, created.text

    run = run_to_completion(admin, created.json()["id"], "SN-FLASH-0002")
    assert run["status"] == "FAIL"
    item = run["items"][0]
    assert item["status"] == "FAIL"
    assert "仿真烧录在阶段“校验”失败" in item["message"]
    assert len(run["flash_records"]) == 1
    assert run["flash_records"][0]["ok"] is False
    assert "校验" in run["flash_records"][0]["message"]

    report = admin.get(f"/api/reports/{run['report_id']}").json()
    assert report["status"] == "FAIL"
    assert report["flash_records"][0]["ok"] is False


def test_control_flow_flash_run(flash_env) -> None:
    admin = TestClient(app_module.app)
    login(admin, "admin", "admin123")
    assert admin.post("/api/firmware", json={"source": str(flash_env), "version": "1.0.0"}).status_code == 201
    assert admin.put("/api/flasher/config", json={"backend": "simulator", "config": {"step_delay_ms": 0}}).status_code == 200
    definition = {
        "schema_version": 2,
        "name": "控制流烧录",
        "product_model": "FLASH-100",
        "variables": {},
        "steps": [
            {
                "type": "test",
                "id": "flash_node",
                "name": "烧录节点",
                "protocol": "simulator",
                "on_fail": "stop",
                "actions": [
                    {"type": "flash", "firmware": "app_v1.bin", "save_as": "flash_result"},
                    {"type": "assert", "actual": "${flash_result.ok}", "operator": "equals", "expected": True},
                ],
            }
        ],
        "subflows": {},
    }
    created = admin.post("/api/scripts", json={"definition": definition})
    assert created.status_code == 201, created.text

    run = run_to_completion(admin, created.json()["id"], "SN-FLASH-0003")
    assert run["status"] == "PASS", run
    assert len(run["flash_records"]) == 1
    assert run["flash_records"][0]["item_path"] == "root/flash_node"
    assert run["flash_records"][0]["ok"] is True
