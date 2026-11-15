from __future__ import annotations

import json
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .compiler import ScriptValidationError, SUPPORTED_PROTOCOLS, compile_script
from .database import DEFAULT_MES_CONFIG, DEFAULT_SETTINGS, Database
from .executor import TestExecutor
from .mes import MESUploadError, MESUploader, build_payload
from .protocol_definitions import DEFAULT_PROTOCOL_CONFIGS
from .protocols import ProtocolError, create_protocol, protocol_catalog
from .utils import json_dumps, new_id, utc_now


database = Database()
executor = TestExecutor(database)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.initialize()
    database.recover_interrupted_runs()
    yield
    executor.shutdown()


app = FastAPI(
    title="Pilot 生产测试系统",
    description="SN 追溯、低代码测试编排、多协议通讯与 MES 报告上传",
    version="1.0.0",
    lifespan=lifespan,
)


class ScriptPayload(BaseModel):
    definition: dict[str, Any]


class SNPayload(BaseModel):
    sn: str = Field(min_length=1, max_length=128)
    script_id: int


class StartPayload(SNPayload):
    operator: str | None = Field(default=None, max_length=64)


class ConfigPayload(BaseModel):
    config: dict[str, Any]


class SettingsPayload(BaseModel):
    station_id: str = Field(min_length=1, max_length=64)
    operator: str = Field(min_length=1, max_length=64)
    sn_pattern: str = Field(min_length=1, max_length=500)
    auto_upload: bool = False


def _script(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "product_model": row["product_model"],
        "version": row["version"],
        "enabled": bool(row["enabled"]),
        "definition": json.loads(row["definition_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _get_script(script_id: int, enabled_only: bool = True) -> sqlite3.Row:
    row = database.one("SELECT * FROM scripts WHERE id = ?", (script_id,))
    if row is None or (enabled_only and not row["enabled"]):
        raise HTTPException(404, "测试脚本不存在或已停用")
    return row


def _validate_sn(sn: str) -> str:
    sn = sn.strip()
    pattern = database.setting("sn_pattern", DEFAULT_SETTINGS["sn_pattern"])
    try:
        matched = re.fullmatch(pattern, sn) is not None
    except re.error:
        raise HTTPException(500, "SN 校验规则配置无效")
    if not matched:
        raise HTTPException(422, f"SN 格式不符合规则：{pattern}")
    return sn


def _run(run_id: str) -> dict[str, Any]:
    row = database.one("SELECT * FROM test_runs WHERE id = ?", (run_id,))
    if row is None:
        raise HTTPException(404, "测试运行记录不存在")
    items = database.all("SELECT * FROM test_items WHERE run_id = ? ORDER BY sequence", (run_id,))
    report = database.one("SELECT id, report_no, mes_status FROM reports WHERE run_id = ?", (run_id,))
    return {
        "id": row["id"],
        "sn": row["sn"],
        "product_model": row["product_model"],
        "script_id": row["script_id"],
        "script_version": row["script_version"],
        "station": row["station"],
        "operator": row["operator"],
        "status": row["status"],
        "current_item": row["current_item"],
        "error": row["error"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "report_id": report["id"] if report else None,
        "report_no": report["report_no"] if report else None,
        "mes_status": report["mes_status"] if report else None,
        "items": [
            {
                "item_id": item["item_id"],
                "name": item["item_name"],
                "sequence": item["sequence"],
                "status": item["status"],
                "expected": item["expected"],
                "actual": item["actual"],
                "message": item["message"],
                "attempts": item["attempts"],
                "assertions": json.loads(item["details_json"]),
                "started_at": item["started_at"],
                "finished_at": item["finished_at"],
                "duration_ms": item["duration_ms"],
            }
            for item in items
        ],
    }


def _report_list_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "report_no": row["report_no"],
        "sn": row["sn"],
        "product_model": row["product_model"],
        "station": row["station"],
        "operator": row["operator"],
        "script_name": row["script_name"],
        "script_version": row["script_version"],
        "status": row["status"],
        "summary": json.loads(row["summary_json"]),
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "duration_ms": row["duration_ms"],
        "mes_status": row["mes_status"],
        "mes_attempts": row["mes_attempts"],
        "mes_response": row["mes_response"],
        "uploaded_at": row["uploaded_at"],
    }


def _validate_mes_config(config: dict[str, Any]) -> None:
    if not str(config.get("endpoint", "")).startswith(("http://", "https://")):
        raise HTTPException(422, "MES 地址必须是 HTTP/HTTPS URL")
    if str(config.get("method", "POST")).upper() not in {"POST", "PUT", "PATCH"}:
        raise HTTPException(422, "MES 请求方法仅支持 POST、PUT、PATCH")
    if not isinstance(config.get("headers", {}), dict):
        raise HTTPException(422, "MES headers 必须是 JSON 对象")
    if config.get("payload_mode", "mapped") not in {"full", "mapped"}:
        raise HTTPException(422, "payload_mode 仅支持 full 或 mapped")
    if config.get("payload_mode", "mapped") == "mapped" and not isinstance(
        config.get("payload_mapping"), (dict, list)
    ):
        raise HTTPException(422, "payload_mapping 必须是 JSON 对象或数组")
    timeout = config.get("timeout_seconds", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120:
        raise HTTPException(422, "MES 超时时间必须为 1~120 秒")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "time": utc_now(), "station": database.setting("station_id")}


@app.get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    totals = database.one(
        "SELECT COUNT(*) AS total, SUM(status = 'PASS') AS passed, SUM(status = 'FAIL') AS failed "
        "FROM reports"
    )
    today = database.one(
        "SELECT COUNT(*) AS total, SUM(status = 'PASS') AS passed FROM reports "
        "WHERE substr(finished_at, 1, 10) = substr(?, 1, 10)",
        (utc_now(),),
    )
    recent = database.all("SELECT * FROM reports ORDER BY finished_at DESC LIMIT 8")
    active = database.one("SELECT id FROM test_runs WHERE status = 'RUNNING' ORDER BY started_at DESC LIMIT 1")
    return {
        "total": totals["total"] or 0,
        "passed": totals["passed"] or 0,
        "failed": totals["failed"] or 0,
        "today": today["total"] or 0,
        "today_passed": today["passed"] or 0,
        "yield": round((totals["passed"] or 0) / totals["total"] * 100, 2) if totals["total"] else 0,
        "active_run_id": active["id"] if active else None,
        "recent_reports": [_report_list_item(row) for row in recent],
    }


@app.get("/api/scripts")
def list_scripts() -> list[dict[str, Any]]:
    return [_script(row) for row in database.all("SELECT * FROM scripts ORDER BY enabled DESC, updated_at DESC")]


@app.post("/api/scripts", status_code=201)
def create_script(payload: ScriptPayload) -> dict[str, Any]:
    try:
        definition = compile_script(payload.definition)
    except ScriptValidationError as exc:
        raise HTTPException(422, {"message": "脚本校验失败", "errors": exc.errors}) from exc
    now = utc_now()
    cursor = None
    with database.transaction() as connection:
        cursor = connection.execute(
            "INSERT INTO scripts(name, product_model, definition_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (definition["name"], definition["product_model"], json_dumps(definition), now, now),
        )
        script_id = cursor.lastrowid
    return _script(_get_script(script_id))


@app.post("/api/scripts/validate")
def validate_script(payload: ScriptPayload) -> dict[str, Any]:
    try:
        compiled = compile_script(payload.definition)
    except ScriptValidationError as exc:
        return {"valid": False, "errors": exc.errors}
    return {"valid": True, "message": f"校验通过，共 {len(compiled['steps'])} 个测试项", "compiled": compiled}


@app.put("/api/scripts/{script_id}")
def update_script(script_id: int, payload: ScriptPayload) -> dict[str, Any]:
    current = _get_script(script_id, enabled_only=False)
    try:
        definition = compile_script(payload.definition)
    except ScriptValidationError as exc:
        raise HTTPException(422, {"message": "脚本校验失败", "errors": exc.errors}) from exc
    database.execute(
        "UPDATE scripts SET name = ?, product_model = ?, version = version + 1, enabled = 1, "
        "definition_json = ?, updated_at = ? WHERE id = ?",
        (
            definition["name"],
            definition["product_model"],
            json_dumps(definition),
            utc_now(),
            script_id,
        ),
    )
    return _script(_get_script(script_id))


@app.delete("/api/scripts/{script_id}")
def disable_script(script_id: int) -> dict[str, str]:
    _get_script(script_id)
    database.execute("UPDATE scripts SET enabled = 0, updated_at = ? WHERE id = ?", (utc_now(), script_id))
    return {"message": "脚本已停用，历史报告仍保留原脚本快照"}


@app.get("/api/protocols")
def get_protocols() -> list[dict[str, Any]]:
    configs = {row["name"]: json.loads(row["config_json"]) for row in database.all("SELECT * FROM protocol_configs")}
    return [{**item, "config": configs.get(item["name"], {})} for item in protocol_catalog()]


@app.put("/api/protocols/{name}")
def save_protocol(name: str, payload: ConfigPayload) -> dict[str, Any]:
    if name not in SUPPORTED_PROTOCOLS:
        raise HTTPException(404, "通讯协议不存在")
    database.execute(
        "INSERT INTO protocol_configs(name, config_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET config_json = excluded.config_json, updated_at = excluded.updated_at",
        (name, json_dumps(payload.config), utc_now()),
    )
    return {"message": "通讯配置已保存", "name": name, "config": payload.config}


@app.post("/api/protocols/{name}/test")
def test_protocol(name: str, payload: ConfigPayload) -> dict[str, Any]:
    if name not in SUPPORTED_PROTOCOLS:
        raise HTTPException(404, "通讯协议不存在")
    protocol = create_protocol(name, payload.config)
    try:
        return protocol.test_connection()
    except (ProtocolError, KeyError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        try:
            protocol.close()
        except Exception:
            pass


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return {key: database.setting(key, value) for key, value in DEFAULT_SETTINGS.items()}


@app.put("/api/settings")
def save_settings(payload: SettingsPayload) -> dict[str, Any]:
    try:
        re.compile(payload.sn_pattern)
    except re.error as exc:
        raise HTTPException(422, f"SN 正则表达式无效：{exc}") from exc
    for key, value in payload.model_dump().items():
        database.save_setting(key, value.strip() if isinstance(value, str) else value)
    return get_settings()


@app.get("/api/mes/config")
def get_mes_config() -> dict[str, Any]:
    return database.setting("mes", DEFAULT_MES_CONFIG)


@app.put("/api/mes/config")
def save_mes_config(payload: ConfigPayload) -> dict[str, Any]:
    _validate_mes_config(payload.config)
    database.save_setting("mes", payload.config)
    return payload.config


@app.get("/api/mes/preview")
def preview_mes_payload() -> dict[str, Any]:
    row = database.one("SELECT report_json FROM reports ORDER BY finished_at DESC LIMIT 1")
    if row:
        report = json.loads(row["report_json"])
    else:
        report = {
            "schema_version": "1.0",
            "report_no": "RPT-YYYYMMDD-DEMO",
            "sn": "SN-DEMO-0001",
            "product_model": "DEMO-100",
            "station": database.setting("station_id"),
            "operator": database.setting("operator"),
            "script": {"id": 1, "name": "示例脚本", "version": 1},
            "status": "PASS",
            "started_at": utc_now(),
            "finished_at": utc_now(),
            "duration_ms": 1200,
            "summary": {"total": 1, "passed": 1, "failed": 0},
            "items": [{"item_id": "self_test", "name": "产品功能自检", "status": "PASS", "actual": "PASS"}],
        }
    try:
        return build_payload(report, database.setting("mes", DEFAULT_MES_CONFIG))
    except MESUploadError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/test-runs/scan")
def scan_sn(payload: SNPayload) -> dict[str, Any]:
    sn = _validate_sn(payload.sn)
    script = _get_script(payload.script_id)
    now = utc_now()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO products(sn, product_model, first_seen_at) VALUES (?, ?, ?) "
            "ON CONFLICT(sn) DO UPDATE SET product_model = excluded.product_model",
            (sn, script["product_model"], now),
        )
    report = database.one("SELECT id, report_no, status FROM reports WHERE sn = ?", (sn,))
    active = database.one(
        "SELECT id, sn FROM test_runs WHERE status = 'RUNNING' AND station = ?",
        (database.setting("station_id"),),
    )
    result = {
        "sn": sn,
        "product_model": script["product_model"],
        "can_start": report is None and active is None,
        "report": {"id": report["id"], "report_no": report["report_no"], "status": report["status"]} if report else None,
        "active_run_id": active["id"] if active else None,
        "message": "SN 校验通过，可以开始测试",
    }
    if report:
        result["message"] = "该 SN 已生成报告，禁止重复测试"
    elif active:
        result["message"] = f"工位正在测试 {active['sn']}，请等待完成"
    return result


@app.post("/api/test-runs", status_code=201)
def start_test(payload: StartPayload) -> dict[str, Any]:
    sn = _validate_sn(payload.sn)
    script = _get_script(payload.script_id)
    station = str(database.setting("station_id", DEFAULT_SETTINGS["station_id"]))
    operator = (payload.operator or database.setting("operator", "OP-001")).strip()
    if not operator:
        raise HTTPException(422, "操作员不能为空")
    run_id = new_id("run")
    started_at = utc_now()
    definition = json.loads(script["definition_json"])
    try:
        with database.transaction() as connection:
            if connection.execute("SELECT 1 FROM reports WHERE sn = ?", (sn,)).fetchone():
                raise HTTPException(409, "该 SN 已存在一对一报告，禁止重复测试")
            if connection.execute(
                "SELECT 1 FROM test_runs WHERE station = ? AND status = 'RUNNING'", (station,)
            ).fetchone():
                raise HTTPException(409, "当前工位有测试正在执行")
            if connection.execute("SELECT 1 FROM products WHERE sn = ?", (sn,)).fetchone() is None:
                raise HTTPException(422, "请先扫描并绑定 SN，再开始测试")
            connection.execute(
                "UPDATE products SET product_model = ? WHERE sn = ?",
                (script["product_model"], sn),
            )
            connection.execute(
                "INSERT INTO test_runs(id, sn, product_model, script_id, script_version, script_snapshot_json, "
                "station, operator, status, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?)",
                (
                    run_id,
                    sn,
                    script["product_model"],
                    script["id"],
                    script["version"],
                    script["definition_json"],
                    station,
                    operator,
                    started_at,
                ),
            )
            for sequence, step in enumerate(definition["steps"]):
                connection.execute(
                    "INSERT INTO test_items(run_id, item_id, item_name, sequence, status) VALUES (?, ?, ?, ?, 'PENDING')",
                    (run_id, step["id"], step["name"], sequence),
                )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "SN 或工位状态冲突，请刷新后重试") from exc
    executor.submit(run_id)
    return _run(run_id)


@app.get("/api/test-runs/active")
def active_run() -> dict[str, Any] | None:
    row = database.one("SELECT id FROM test_runs WHERE status = 'RUNNING' ORDER BY started_at DESC LIMIT 1")
    return _run(row["id"]) if row else None


@app.get("/api/test-runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    return _run(run_id)


@app.get("/api/reports")
def list_reports(
    sn: str = "",
    status: str = "",
    mes_status: str = "",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    conditions: list[str] = []
    params: list[Any] = []
    if sn.strip():
        conditions.append("sn LIKE ?")
        params.append(f"%{sn.strip()}%")
    if status:
        if status not in {"PASS", "FAIL"}:
            raise HTTPException(422, "报告状态筛选值无效")
        conditions.append("status = ?")
        params.append(status)
    if mes_status:
        if mes_status not in {"PENDING", "UPLOADING", "SUCCESS", "FAILED"}:
            raise HTTPException(422, "MES 状态筛选值无效")
        conditions.append("mes_status = ?")
        params.append(mes_status)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    total = database.one(f"SELECT COUNT(*) AS count FROM reports {where}", tuple(params))["count"]
    rows = database.all(
        f"SELECT * FROM reports {where} ORDER BY finished_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    return {"total": total, "items": [_report_list_item(row) for row in rows]}


@app.get("/api/reports/{report_id}")
def get_report(report_id: str) -> dict[str, Any]:
    row = database.one("SELECT * FROM reports WHERE id = ?", (report_id,))
    if row is None:
        raise HTTPException(404, "报告不存在")
    report = json.loads(row["report_json"])
    report.update(_report_list_item(row))
    return report


@app.get("/api/reports/{report_id}/download")
def download_report(report_id: str) -> JSONResponse:
    row = database.one("SELECT report_no, report_json FROM reports WHERE id = ?", (report_id,))
    if row is None:
        raise HTTPException(404, "报告不存在")
    return JSONResponse(
        content=json.loads(row["report_json"]),
        headers={"Content-Disposition": f'attachment; filename="{row["report_no"]}.json"'},
    )


@app.get("/api/reports/{report_id}/upload-logs")
def get_upload_logs(report_id: str) -> list[dict[str, Any]]:
    rows = database.all(
        "SELECT * FROM mes_upload_logs WHERE report_id = ? ORDER BY id DESC", (report_id,)
    )
    return [
        {
            "attempt": row["attempt"],
            "success": bool(row["success"]),
            "request": json.loads(row["request_json"]),
            "response": row["response"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


@app.post("/api/reports/{report_id}/upload")
def upload_report(report_id: str) -> dict[str, Any]:
    try:
        return MESUploader(database).upload(report_id)
    except MESUploadError as exc:
        raise HTTPException(502, str(exc)) from exc


STATIC_DIR = Path(__file__).with_name("static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
