from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import testsystem.app as app_module
import testsystem.mes_master as mes_master
from testsystem.database import Database


@pytest.fixture
def isolated_api(tmp_path, monkeypatch):
    database = Database(tmp_path / "master-test.db")
    database.initialize()
    monkeypatch.setattr(app_module, "database", database)
    monkeypatch.setattr(app_module.executor, "db", database)
    monkeypatch.setattr(app_module.auth_service, "db", database)
    return database


def login(client: TestClient, username: str, password: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text


def admin_client() -> TestClient:
    client = TestClient(app_module.app)
    login(client, "admin", "admin123")
    return client


def operator_client() -> TestClient:
    client = TestClient(app_module.app)
    login(client, "operator", "operator123")
    return client


def sync(client: TestClient, target: str = "both") -> dict:
    response = client.post("/api/mes/sync", json={"target": target})
    assert response.status_code == 200, response.text
    return response.json()


def script_definition(product_model: str, station_code: str = "") -> dict:
    return {
        "schema_version": 1,
        "name": f"{product_model} 测试流程",
        "product_model": product_model,
        "station_code": station_code,
        "variables": {},
        "steps": [
            {
                "id": "self_check",
                "name": "产品功能自检",
                "protocol": "simulator",
                "timeout_ms": 5000,
                "retries": 0,
                "on_fail": "stop",
                "actions": [
                    {"type": "request", "data": "AT+SELFTEST?", "save_as": "result"},
                    {"type": "assert", "actual": "${result}", "operator": "equals", "expected": "PASS"},
                ],
            }
        ],
    }


def test_simulator_sync_populates_master_data(isolated_api: Database) -> None:
    client = admin_client()
    result = sync(client)
    assert result["products"]["added"] == len(mes_master.SIMULATOR_PRODUCTS)
    assert result["stations"]["added"] == len(mes_master.SIMULATOR_STATIONS)

    products = client.get("/api/mes/products").json()
    stations = client.get("/api/mes/stations").json()
    assert products["mode"] == "simulator"
    assert products["last_sync"] and stations["last_sync"]
    product_codes = {item["code"]: item for item in products["items"]}
    assert product_codes["DEMO-100"]["source"] == "mes"
    assert product_codes["DEMO-100"]["enabled"] is True
    station_codes = {item["code"] for item in stations["items"]}
    assert {"STATION-01", "STATION-02", "ICT-01"} <= station_codes

    # 二次同步变为更新，不产生新增
    again = sync(client, "products")["products"]
    assert again["added"] == 0 and again["updated"] == len(mes_master.SIMULATOR_PRODUCTS)


def test_local_crud_and_sync_isolation(isolated_api: Database) -> None:
    client = admin_client()
    created = client.post("/api/mes/products", json={"code": "LOC-1", "name": "本地产品", "spec": "L1"})
    assert created.status_code == 201, created.text
    assert created.json()["item"]["source"] == "local"
    assert client.post("/api/mes/products", json={"code": "LOC-1", "name": "重复"}).status_code == 409

    station = client.post("/api/mes/stations", json={"code": "LOC-ST", "name": "本地工站", "line": "L9"})
    assert station.status_code == 201, station.text

    # 同步不影响本地数据；同编号时以 MES 数据覆盖
    sync(client)
    products = {item["code"]: item for item in client.get("/api/mes/products").json()["items"]}
    assert products["LOC-1"]["source"] == "local" and products["LOC-1"]["enabled"] is True
    assert products["DEMO-100"]["source"] == "mes"

    # MES 同步的数据只能启停，不能改名称或删除
    rejected = client.put("/api/mes/products/DEMO-100", json={"name": "改名"})
    assert rejected.status_code == 422
    assert client.delete("/api/mes/products/DEMO-100").status_code == 409
    toggled = client.put("/api/mes/products/DEMO-100", json={"enabled": False})
    assert toggled.status_code == 200
    assert toggled.json()["item"]["enabled"] is False

    # 本地数据可编辑、可删除
    updated = client.put("/api/mes/products/LOC-1", json={"name": "改名后", "spec": "L2"})
    assert updated.status_code == 200
    assert updated.json()["item"]["name"] == "改名后"
    assert client.delete("/api/mes/products/LOC-1").status_code == 200
    assert client.delete("/api/mes/stations/LOC-ST").status_code == 200
    assert client.put("/api/mes/products/LOC-1", json={"enabled": True}).status_code == 404


def test_sync_disables_mes_rows_missing_from_response(isolated_api: Database, monkeypatch) -> None:
    client = admin_client()
    sync(client)
    original_total = len(mes_master.SIMULATOR_PRODUCTS)
    reduced = [mes_master.SIMULATOR_PRODUCTS[0]]
    monkeypatch.setattr(mes_master, "SIMULATOR_PRODUCTS", reduced)
    result = sync(client, "products")["products"]
    assert result["disabled"] == original_total - len(reduced)
    products = {item["code"]: item for item in client.get("/api/mes/products").json()["items"]}
    assert products["DEMO-100"]["enabled"] is True
    assert products["SIM-200"]["enabled"] is False
    # 本地行不受同步影响
    client.post("/api/mes/products", json={"code": "LOC-9", "name": "本地"})
    sync(client, "products")
    products = {item["code"]: item for item in client.get("/api/mes/products").json()["items"]}
    assert products["LOC-9"]["enabled"] is True


def test_script_binding_validation(isolated_api: Database) -> None:
    client = admin_client()
    # 主数据为空时保持自由文本（向后兼容）
    free = client.post("/api/scripts", json={"definition": script_definition("WHATEVER-1")})
    assert free.status_code == 201, free.text

    sync(client)
    unknown = client.post("/api/scripts", json={"definition": script_definition("NOT-IN-MES")})
    assert unknown.status_code == 422
    client.put("/api/mes/products/SIM-300", json={"enabled": False})
    rejected = client.post("/api/scripts", json={"definition": script_definition("SIM-300")})
    assert rejected.status_code == 422
    client.put("/api/mes/products/SIM-300", json={"enabled": True})

    unknown_station = client.post("/api/scripts", json={"definition": script_definition("SIM-200", "NO-SUCH")})
    assert unknown_station.status_code == 422

    first = client.post("/api/scripts", json={"definition": script_definition("SIM-200", "ICT-01")})
    assert first.status_code == 201, first.text
    assert first.json()["station_code"] == "ICT-01"
    duplicate = client.post("/api/scripts", json={"definition": script_definition("SIM-200", "ICT-01")})
    assert duplicate.status_code == 409
    # 同产品不同工站可以各建流程
    other_station = client.post("/api/scripts", json={"definition": script_definition("SIM-200", "STATION-01")})
    assert other_station.status_code == 201
    # 未绑定工站的流程保持原有自由模式，允许同产品多个启用流程
    unbound = client.post("/api/scripts", json={"definition": script_definition("SIM-200")})
    assert unbound.status_code == 201
    duplicate_unbound = client.post("/api/scripts", json={"definition": script_definition("SIM-200")})
    assert duplicate_unbound.status_code == 201
    default_like = client.post("/api/scripts", json={"definition": script_definition("DEMO-100")})
    assert default_like.status_code == 201

    # 更新脚本排除自身，仅修改名称不产生冲突
    script_id = first.json()["id"]
    renamed = dict(script_definition("SIM-200", "ICT-01"), name="ICT 流程 v2")
    updated = client.put(f"/api/scripts/{script_id}", json={"definition": renamed})
    assert updated.status_code == 200


def test_start_test_rejects_station_mismatch(isolated_api: Database) -> None:
    admin = admin_client()
    sync(admin)
    created = admin.post("/api/scripts", json={"definition": script_definition("DEMO-100", "ICT-01")})
    assert created.status_code == 201, created.text
    script_id = created.json()["id"]

    operator = operator_client()
    scan = operator.post("/api/test-runs/scan", json={"sn": "SN-ICT-0001", "script_id": script_id})
    assert scan.status_code == 200, scan.text
    started = operator.post("/api/test-runs", json={"sn": "SN-ICT-0001", "script_id": script_id})
    assert started.status_code == 409
    assert "ICT-01" in started.text

    # 当前工站切换到 ICT-01 后允许执行，且未绑定工站的旧流程不受影响
    settings = admin.get("/api/settings").json()
    settings["station_id"] = "ICT-01"
    saved = admin.put("/api/settings", json=settings)
    assert saved.status_code == 200, saved.text
    scan = operator.post("/api/test-runs/scan", json={"sn": "SN-ICT-0001", "script_id": script_id})
    assert scan.json()["can_start"] is True
    started = operator.post("/api/test-runs", json={"sn": "SN-ICT-0001", "script_id": script_id})
    assert started.status_code == 201, started.text


def test_operator_master_data_permissions(isolated_api: Database) -> None:
    operator = operator_client()
    assert operator.get("/api/mes/products").status_code == 200
    assert operator.get("/api/mes/stations").status_code == 200
    assert operator.post("/api/mes/sync", json={"target": "both"}).status_code == 403
    assert operator.post("/api/mes/products", json={"code": "X", "name": "X"}).status_code == 403
    assert operator.put("/api/mes/products/DEMO-100", json={"enabled": False}).status_code == 403
    assert operator.delete("/api/mes/products/DEMO-100").status_code == 403


def test_mes_config_merge_adds_master_data_defaults(isolated_api: Database) -> None:
    old_config = {
        "endpoint": "http://127.0.0.1:9000/api/mes/test-reports",
        "method": "POST",
        "timeout_seconds": 10,
        "headers": {},
        "idempotency_header": "Idempotency-Key",
        "payload_mode": "mapped",
        "payload_mapping": {"event": "production_test.completed"},
    }
    isolated_api.save_setting("mes", old_config)
    isolated_api.initialize()
    merged = isolated_api.setting("mes")
    assert merged["endpoint"] == old_config["endpoint"]
    assert merged["master_data"]["mode"] == "simulator"
    assert merged["master_data"]["products"]["field_map"]["code"] == "itemCode"
    assert merged["master_data"]["stations"]["field_map"]["station_type"] == "stationType"

    # 保存 master_data 后再次初始化不覆盖用户配置
    merged["master_data"]["mode"] = "http"
    merged["master_data"]["products"]["url"] = "http://mes.example.com/api/products"
    isolated_api.save_setting("mes", merged)
    isolated_api.initialize()
    assert isolated_api.setting("mes")["master_data"]["mode"] == "http"
    assert isolated_api.setting("mes")["master_data"]["products"]["url"] == "http://mes.example.com/api/products"


def test_save_mes_config_validates_master_data(isolated_api: Database) -> None:
    client = admin_client()
    config = client.get("/api/mes/config").json()
    config["master_data"]["mode"] = "http"
    config["master_data"]["products"]["url"] = "not-a-url"
    assert client.put("/api/mes/config", json={"config": config}).status_code == 422
    config["master_data"]["products"]["url"] = "http://mes.example.com/api/products"
    config["master_data"]["stations"]["url"] = "http://mes.example.com/api/stations"
    assert client.put("/api/mes/config", json={"config": config}).status_code == 200
