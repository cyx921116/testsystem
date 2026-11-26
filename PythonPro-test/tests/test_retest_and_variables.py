from __future__ import annotations

import sqlite3
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
    # 失败数据不可清除：两次失败的报告都必须保留在本地，且各自独立
    reports = admin.get(f"/api/reports?sn={sn}").json()["items"]
    assert len(reports) == 2
    assert {item["id"] for item in reports} >= {first_report_id}
    assert first_report_id in {item["id"] for item in reports}
    assert all(item["status"] == "FAIL" for item in reports)

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


def failing_definition(name: str, max_retests: int | None = None) -> dict:
    """构造一个必然失败的流程定义，可选携带流程级复测次数上限。"""
    definition = simple_script(name, [
        {"type": "assert", "actual": "1", "operator": "equals", "expected": "2", "message": "故意失败"},
    ])
    if max_retests is not None:
        definition["max_retests"] = max_retests
    return definition


def test_retest_limit_counts_across_all_scripts(isolated_api: Database) -> None:
    """复测次数按 SN 跨全部测试流程累计：换流程不能绕过失败次数限制。"""
    admin = TestClient(app_module.app)
    login(admin, "admin", "admin123")
    settings = admin.get("/api/settings").json()
    admin.put("/api/settings", json={
        "station_id": "STATION-01", "operator": "OP-001",
        "sn_pattern": settings["sn_pattern"], "auto_upload": False, "max_retests": 1,
    })
    script_a = admin.post("/api/scripts", json={"definition": failing_definition("流程A")}).json()["id"]
    script_b = admin.post("/api/scripts", json={"definition": failing_definition("流程B")}).json()["id"]

    sn = "SN-CROSS-0001"
    assert run_to_completion(admin, script_a, sn)["status"] == "FAIL"

    # 换另一个流程扫码：次数应累计为第 2 次（上限 = 1 次初测 + 1 次复测）
    scan = admin.post("/api/test-runs/scan", json={"sn": sn, "script_id": script_b}).json()
    assert scan["can_start"] is True
    assert scan["attempt"] == 2
    assert scan["max_attempts"] == 2
    assert run_to_completion(admin, script_b, sn)["status"] == "FAIL"

    # 两个流程各失败一次后，任何流程都不能再测该 SN
    scan = admin.post("/api/test-runs/scan", json={"sn": sn, "script_id": script_a}).json()
    assert scan["can_start"] is False
    assert "复测次数已用完" in scan["message"]
    assert admin.post("/api/test-runs", json={"sn": sn, "script_id": script_b}).status_code == 409
    # 两份失败报告全部保留
    assert len(admin.get(f"/api/reports?sn={sn}").json()["items"]) == 2


def test_script_level_max_retests_overrides_station_setting(isolated_api: Database) -> None:
    """流程定义的 max_retests 优先于工位策略；未配置时跟随工位策略。"""
    admin = TestClient(app_module.app)
    login(admin, "admin", "admin123")
    settings = admin.get("/api/settings").json()
    admin.put("/api/settings", json={
        "station_id": "STATION-01", "operator": "OP-001",
        "sn_pattern": settings["sn_pattern"], "auto_upload": False, "max_retests": 3,
    })
    strict_id = admin.post(
        "/api/scripts", json={"definition": failing_definition("禁止复测流程", max_retests=0)}
    ).json()["id"]
    follow_id = admin.post(
        "/api/scripts", json={"definition": failing_definition("跟随工位流程")}
    ).json()["id"]

    # 流程级 max_retests=0：失败一次即禁止复测（即使工位策略允许 3 次）
    sn_strict = "SN-STRICT-0001"
    scan = admin.post("/api/test-runs/scan", json={"sn": sn_strict, "script_id": strict_id}).json()
    assert scan["max_attempts"] == 1
    assert run_to_completion(admin, strict_id, sn_strict)["status"] == "FAIL"
    scan = admin.post("/api/test-runs/scan", json={"sn": sn_strict, "script_id": strict_id}).json()
    assert scan["can_start"] is False
    assert "复测次数已用完" in scan["message"]
    assert admin.post("/api/test-runs", json={"sn": sn_strict, "script_id": strict_id}).status_code == 409

    # 未配置 max_retests 的流程：跟随工位策略 1 + 3
    scan = admin.post("/api/test-runs/scan", json={"sn": "SN-FOLLOW-0001", "script_id": follow_id}).json()
    assert scan["max_attempts"] == 4

    # 非法 max_retests 在编译校验阶段被拒绝
    bad = failing_definition("非法复测次数", max_retests=99)
    assert admin.post("/api/scripts", json={"definition": bad}).status_code == 422


def test_failure_reports_kept_and_uploaded_to_mes(isolated_api: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """开启自动上传后：失败报告与通过报告都先保存本地，再各自上传 MES，失败数据不清除。"""
    from testsystem.mes import MESUploader

    sent: list[dict] = []

    def fake_send(self, payload, report, config):  # noqa: ANN001, ANN202
        sent.append({"report_no": report["report_no"], "status": report["status"]})
        return "OK"

    monkeypatch.setattr(MESUploader, "_send", fake_send)

    admin = TestClient(app_module.app)
    login(admin, "admin", "admin123")
    settings = admin.get("/api/settings").json()
    admin.put("/api/settings", json={
        "station_id": "STATION-01", "operator": "OP-001",
        "sn_pattern": settings["sn_pattern"], "auto_upload": True, "max_retests": 3,
    })
    failing_id = admin.post("/api/scripts", json={"definition": failing_definition("失败流程")}).json()["id"]
    passing = simple_script("通过流程", [{"type": "assert", "actual": "PASS", "operator": "equals", "expected": "PASS"}])
    passing_id = admin.post("/api/scripts", json={"definition": passing}).json()["id"]

    sn = "SN-KEEP-0001"
    assert run_to_completion(admin, failing_id, sn)["status"] == "FAIL"
    assert run_to_completion(admin, passing_id, sn)["status"] == "PASS"

    # 失败报告在复测通过后仍保留：共 2 份（1 FAIL + 1 PASS），且都已自动上传 MES
    reports = admin.get(f"/api/reports?sn={sn}").json()["items"]
    assert len(reports) == 2
    by_status = {item["status"]: item for item in reports}
    assert set(by_status) == {"FAIL", "PASS"}
    assert all(item["mes_status"] == "SUCCESS" for item in reports)
    assert {entry["status"] for entry in sent} == {"FAIL", "PASS"}
    # 上传日志也随报告留档
    logs = admin.get(f"/api/reports/{by_status['FAIL']['id']}/upload-logs").json()
    assert len(logs) == 1 and logs[0]["success"] is True


def test_reports_table_migration_keeps_history(tmp_path) -> None:
    """旧库（reports.sn 带 UNIQUE）升级后：唯一约束移除、历史报告与上传日志完整保留。"""
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE reports (
            id TEXT PRIMARY KEY,
            report_no TEXT NOT NULL UNIQUE,
            sn TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL UNIQUE,
            product_model TEXT NOT NULL,
            station TEXT NOT NULL,
            operator TEXT NOT NULL,
            script_name TEXT NOT NULL,
            script_version INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('PASS', 'FAIL')),
            summary_json TEXT NOT NULL,
            report_json TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            mes_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(mes_status IN ('PENDING', 'UPLOADING', 'SUCCESS', 'FAILED')),
            mes_attempts INTEGER NOT NULL DEFAULT 0,
            mes_response TEXT,
            uploaded_at TEXT
        );
        CREATE TABLE mes_upload_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            request_json TEXT NOT NULL,
            success INTEGER NOT NULL,
            response TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(report_id) REFERENCES reports(id) ON DELETE CASCADE
        );
        INSERT INTO reports(id, report_no, sn, run_id, product_model, station, operator, script_name,
                            script_version, status, summary_json, report_json, started_at, finished_at, duration_ms)
        VALUES ('report_old', 'RPT-OLD-1', 'SN-OLD-1', 'run_old', 'M-1', 'STATION-01', 'op', '旧流程',
                1, 'FAIL', '{}', '{}', '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z', 60000);
        INSERT INTO mes_upload_logs(report_id, attempt, request_json, success, response, created_at)
        VALUES ('report_old', 1, '{}', 1, 'OK', '2026-01-01T00:02:00Z');
        """
    )
    legacy.commit()
    legacy.close()

    database = Database(db_path)
    database.initialize()

    # 历史失败报告与上传日志在迁移后完好保留
    assert database.one("SELECT status FROM reports WHERE id = 'report_old'")["status"] == "FAIL"
    assert database.one("SELECT COUNT(*) AS c FROM mes_upload_logs")["c"] == 1
    # 同一 SN 现在允许保存多份报告（先补齐外键引用的产品与新运行记录）
    database.execute(
        "INSERT INTO products(sn, product_model, first_seen_at) VALUES ('SN-OLD-1', 'M-1', '2026-01-02T00:00:00Z')"
    )
    database.execute(
        "INSERT INTO test_runs(id, sn, product_model, script_id, script_version, script_snapshot_json, "
        "station, operator, status, started_at, finished_at) VALUES "
        "('run_new', 'SN-OLD-1', 'M-1', 1, 2, '{}', 'STATION-01', 'op', 'PASS', "
        "'2026-01-02T00:00:00Z', '2026-01-02T00:01:00Z')"
    )
    database.execute(
        "INSERT INTO reports(id, report_no, sn, run_id, product_model, station, operator, script_name, "
        "script_version, status, summary_json, report_json, started_at, finished_at, duration_ms) "
        "VALUES ('report_new', 'RPT-NEW-1', 'SN-OLD-1', 'run_new', 'M-1', 'STATION-01', 'op', '新流程', "
        "2, 'PASS', '{}', '{}', '2026-01-02T00:00:00Z', '2026-01-02T00:01:00Z', 60000)"
    )
    assert database.one("SELECT COUNT(*) AS c FROM reports WHERE sn = 'SN-OLD-1'")["c"] == 2
