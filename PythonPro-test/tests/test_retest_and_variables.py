from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import testsystem.app as app_module
from testsystem.database import Database


@pytest.fixture
def isolated_api(tmp_path, monkeypatch):
    database = Database(tmp_path / "retest-test.db")
    database.initialize()
    monkeypatch.setattr(app_module, "database", database)
    monkeypatch.setattr(app_module.executor, "db", database)
    monkeypatch.setattr(app_module.auth_service, "db", database)
    return database


def login(client: TestClient, username: str, password: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text


def simple_script(name: str, actions: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "name": name,
        "product_model": "RETEST-100",
        "variables": {},
        "steps": [
            {
                "id": "step1",
                "name": "判定步骤",
                "protocol": "simulator",
                "timeout_ms": 10000,
                "retries": 0,
                "on_fail": "stop",
                "actions": actions,
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


def test_failed_sn_can_retest_within_limit(isolated_api: Database) -> None:
    admin = TestClient(app_module.app)
    login(admin, "admin", "admin123")
    settings = admin.get("/api/settings").json()
    assert settings["max_retests"] == 3
    saved = admin.put("/api/settings", json={
        "station_id": "STATION-01", "operator": "OP-001",
        "sn_pattern": settings["sn_pattern"], "auto_upload": False, "max_retests": 1,
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["max_retests"] == 1

    failing = admin.post("/api/scripts", json={"definition": simple_script("必失败流程", [
        {"type": "assert", "actual": "1", "operator": "equals", "expected": "2", "message": "故意失败"},
    ])})
    assert failing.status_code == 201, failing.text
    failing_id = failing.json()["id"]
    passing = admin.post("/api/scripts", json={"definition": simple_script("必通过流程", [
        {"type": "assert", "actual": "PASS", "operator": "equals", "expected": "PASS"},
    ])})
    assert passing.status_code == 201, passing.text
    passing_id = passing.json()["id"]

    sn = "SN-RETEST-0001"
    scan = admin.post("/api/test-runs/scan", json={"sn": sn, "script_id": failing_id}).json()
    assert scan["can_start"] is True
    assert scan["attempt"] == 1
    assert scan["max_attempts"] == 2

    first = run_to_completion(admin, failing_id, sn)
    assert first["status"] == "FAIL"
    first_report_id = admin.get(f"/api/reports?sn={sn}").json()["items"][0]["id"]

    scan = admin.post("/api/test-runs/scan", json={"sn": sn, "script_id": failing_id}).json()
    assert scan["can_start"] is True
    assert "复测" in scan["message"]
    assert scan["attempt"] == 2

    second = run_to_completion(admin, failing_id, sn)
    assert second["status"] == "FAIL"
    reports = admin.get(f"/api/reports?sn={sn}").json()["items"]
    assert len(reports) == 1
    assert reports[0]["id"] != first_report_id

    scan = admin.post("/api/test-runs/scan", json={"sn": sn, "script_id": failing_id}).json()
    assert scan["can_start"] is False
    assert "复测次数已用完" in scan["message"]
    blocked = admin.post("/api/test-runs", json={"sn": sn, "script_id": failing_id})
    assert blocked.status_code == 409
    assert "复测次数已用完" in blocked.text

    sn_pass = "SN-RETEST-0002"
    assert run_to_completion(admin, passing_id, sn_pass)["status"] == "PASS"
    scan = admin.post("/api/test-runs/scan", json={"sn": sn_pass, "script_id": passing_id}).json()
    assert scan["can_start"] is False
    assert "已测试合格" in scan["message"]
    assert admin.post("/api/test-runs", json={"sn": sn_pass, "script_id": passing_id}).status_code == 409


def test_assert_accepts_bare_variable_names(isolated_api: Database) -> None:
    admin = TestClient(app_module.app)
    login(admin, "admin", "admin123")
    definition = simple_script("裸变量断言", [
        {"type": "set", "variable": "voltage", "value": "12.5"},
        {"type": "set", "variable": "result", "value": "PASS"},
        {"type": "assert", "actual": "voltage", "operator": "equals", "expected": "12.5"},
        {"type": "assert", "actual": "result", "operator": "equals", "expected": "PASS"},
        {"type": "assert", "actual": "sn", "operator": "exists"},
        {"type": "assert", "actual": "不存在的变量", "operator": "equals", "expected": "不存在的变量"},
    ])
    created = admin.post("/api/scripts", json={"definition": definition})
    assert created.status_code == 201, created.text

    run = run_to_completion(admin, created.json()["id"], "SN-BARE-0001")
    assert run["status"] == "PASS", run
    assertions = run["items"][0]["assertions"]
    assert assertions[0]["actual"] == "12.5"
    assert assertions[2]["actual"] == "SN-BARE-0001"

    nested = simple_script("裸路径嵌套断言", [
        {"type": "request", "data": "AT+VER?", "save_as": "response"},
        {"type": "assert", "actual": "response", "operator": "exists"},
    ])
    created = admin.post("/api/scripts", json={"definition": nested})
    assert created.status_code == 201, created.text
    run = run_to_completion(admin, created.json()["id"], "SN-BARE-0002")
    assert run["status"] == "PASS", run
