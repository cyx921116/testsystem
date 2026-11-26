from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import testsystem.app as app_module
from testsystem.database import Database


@pytest.fixture
def isolated_api(tmp_path, monkeypatch):
    database = Database(tmp_path / "auth-test.db")
    database.initialize()
    monkeypatch.setattr(app_module, "database", database)
    monkeypatch.setattr(app_module.executor, "db", database)
    monkeypatch.setattr(app_module.auth_service, "db", database)
    return database


def login(client: TestClient, username: str, password: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text


def test_authentication_and_role_permissions(isolated_api: Database) -> None:
    anonymous = TestClient(app_module.app)
    assert anonymous.get("/api/scripts").status_code == 401
    anonymous.cookies.set("pilot_session", "malformed.token")
    assert anonymous.get("/api/scripts").status_code == 401

    operator = TestClient(app_module.app)
    assert operator.post("/api/auth/login", json={"username": "operator", "password": "wrong"}).status_code == 401
    login(operator, "operator", "operator123")
    assert operator.get("/api/auth/me").json()["user"]["role"] == "operator"
    assert operator.get("/api/scripts").status_code == 200
    assert operator.get("/api/protocols").status_code == 403
    assert operator.get("/api/auth/users").status_code == 403
    assert operator.post("/api/scripts/validate", json={"definition": {}}).status_code == 403
    assert operator.put("/api/settings", json={
        "station_id": "STATION-01", "operator": "other", "sn_pattern": ".*", "auto_upload": False,
    }).status_code == 403

    admin = TestClient(app_module.app)
    login(admin, "admin", "admin123")
    assert admin.get("/api/protocols").status_code == 200
    assert admin.get("/api/auth/users").status_code == 200
    created = admin.post("/api/auth/users", json={
        "username": "line_operator",
        "display_name": "产线操作员",
        "role": "operator",
        "password": "linepass123",
    })
    assert created.status_code == 201, created.text
    assert created.json()["role"] == "operator"
    assert admin.post("/api/auth/users", json={
        "username": "line_operator",
        "display_name": "重复用户",
        "role": "operator",
        "password": "linepass123",
    }).status_code == 409
    assert admin.put("/api/auth/users/line_operator/enabled", json={"enabled": False}).status_code == 200
    disabled = TestClient(app_module.app)
    login_response = disabled.post("/api/auth/login", json={"username": "line_operator", "password": "linepass123"})
    assert login_response.status_code == 401


def test_operator_can_complete_test_but_cannot_upload(isolated_api: Database) -> None:
    client = TestClient(app_module.app)
    login(client, "operator", "operator123")
    scripts = client.get("/api/scripts").json()
    script_id = scripts[0]["id"]
    sn = "SN-AUTH-0001"
    scan = client.post("/api/test-runs/scan", json={"sn": sn, "script_id": script_id})
    assert scan.status_code == 200, scan.text
    assert scan.json()["can_start"] is True

    started = client.post("/api/test-runs", json={"sn": sn, "script_id": script_id, "operator": "spoofed"})
    assert started.status_code == 201, started.text
    run = started.json()
    assert run["operator"] == "operator"
    for _ in range(100):
        run = client.get(f"/api/test-runs/{run['id']}").json()
        if run["status"] != "RUNNING":
            break
        time.sleep(0.05)
    assert run["status"] == "PASS", run
    assert all(item["status"] == "PASS" for item in run["items"])

    report = client.get(f"/api/reports/{run['report_id']}")
    assert report.status_code == 200
    assert report.json()["sn"] == sn
    assert client.post(f"/api/reports/{run['report_id']}/upload").status_code == 403


def test_password_change_reissues_valid_session(isolated_api: Database) -> None:
    client = TestClient(app_module.app)
    login(client, "operator", "operator123")
    old_cookie = client.cookies.get("pilot_session")
    changed = client.post("/api/auth/password", json={"current_password": "operator123", "new_password": "newpass123"})
    assert changed.status_code == 200, changed.text
    assert client.get("/api/auth/me").status_code == 200

    old_session = TestClient(app_module.app)
    old_session.cookies.set("pilot_session", old_cookie)
    assert old_session.get("/api/auth/me").status_code == 401
