from __future__ import annotations

import json
import re
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import SESSION_COOKIE, AuthService, create_auth_router
from .compiler import ScriptValidationError, SUPPORTED_PROTOCOLS, compile_script
from .data_codec import DataCodecError, validate_codec_config
from .database import DEFAULT_FLASHER_CONFIG, DEFAULT_MES_CONFIG, DEFAULT_SETTINGS, Database
from .executor import TestExecutor
from .flasher import (
    FLASHER_CLASSES,
    FlashError,
    create_flasher,
    find_firmware,
    flasher_catalog,
    load_firmware_index,
    register_firmware,
)
from .mes import MESUploadError, MESUploader, build_payload
from .mes_master import MESMasterConflictError, MESMasterData, MESMasterError, MESMasterMissingError, MESSyncError
from .protocol_definitions import DEFAULT_PROTOCOL_CONFIGS
from .protocols import ProtocolError, create_protocol, protocol_catalog
from .utils import json_dumps, new_id, utc_now


database = Database()
executor = TestExecutor(database)
auth_service = AuthService(database)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.initialize()
    database.recover_interrupted_runs()
    yield
    executor.shutdown()


app = FastAPI(
    title="Pilot 生产测试系统",
    description="SN 追溯、低代码测试编排、多协议通讯与 MES 报告上传",
    version="0.3.0",
    lifespan=lifespan,
)
app.include_router(create_auth_router(auth_service))


def _operator_allowed(method: str, path: str) -> bool:
    if path in {"/api/auth/me", "/api/auth/password"}:
        return True
    if method == "POST" and path in {"/api/test-runs", "/api/test-runs/scan"}:
        return True
    if method != "GET":
        return False
    return any(
        path == root or path.startswith(f"{root}/")
        for root in (
            "/api/dashboard",
            "/api/scripts",
            "/api/settings",
            "/api/test-runs",
            "/api/reports",
            "/api/mes/products",
            "/api/mes/stations",
        )
    )


@app.middleware("http")
async def authenticate_request(request: Request, call_next: Any) -> Any:
    path = request.url.path
    if not (path == "/api" or path.startswith("/api/")) or path in {"/api/health", "/api/auth/login", "/api/auth/logout"}:
        return await call_next(request)
    user = auth_service.user_from_token(request.cookies.get(SESSION_COOKIE))
    if user is None:
        return JSONResponse(status_code=401, content={"detail": "登录已失效，请重新登录"})
    request.state.user = user
    if user["role"] != "admin" and not _operator_allowed(request.method, path):
        return JSONResponse(status_code=403, content={"detail": "操作工仅可执行测试和查看测试结果"})
    return await call_next(request)


class ScriptPayload(BaseModel):
    definition: dict[str, Any]


class SNPayload(BaseModel):
    sn: str = Field(min_length=1, max_length=128)
    script_id: int
    station: str | None = Field(default=None, max_length=64)


class StartPayload(SNPayload):
    operator: str | None = Field(default=None, max_length=64)


class ConfigPayload(BaseModel):
    config: dict[str, Any]


class SettingsPayload(BaseModel):
    station_id: str = Field(min_length=1, max_length=64)
    operator: str = Field(min_length=1, max_length=64)
    sn_pattern: str = Field(min_length=1, max_length=500)
    auto_upload: bool = False
    max_retests: int = Field(default=3, ge=0, le=20)


class FirmwareRegisterPayload(BaseModel):
    source: str = Field(min_length=1, max_length=500)
    version: str = Field(min_length=1, max_length=64)
    product_model: str = Field(default="", max_length=64)
    description: str = Field(default="", max_length=500)


class FlasherConfigPayload(BaseModel):
    backend: str = Field(min_length=1, max_length=32)
    config: dict[str, Any] = Field(default_factory=dict)


class MasterSyncPayload(BaseModel):
    target: str = Field(default="both", pattern="^(products|stations|both)$")


class MasterItemPayload(BaseModel):
    code: str = Field(default="", max_length=64)
    name: str = Field(default="", max_length=200)
    spec: str = Field(default="", max_length=200)
    line: str = Field(default="", max_length=200)
    station_type: str = Field(default="", max_length=200)
    enabled: bool | None = None


def _script(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "product_model": row["product_model"],
        "station_code": row["station_code"] or "",
        "version": row["version"],
        "enabled": bool(row["enabled"]),
        "definition": json.loads(row["definition_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _validate_script_binding(definition: dict[str, Any], exclude_id: int | None = None) -> str:
    """校验脚本绑定：产品/工站必须来自主数据（主数据非空时）；绑定了工站的流程同产品同工站唯一。"""
    product_model = definition["product_model"]
    station_code = str(definition.get("station_code", "")).strip()
    if database.one("SELECT 1 FROM mes_products LIMIT 1"):
        product = database.one("SELECT enabled FROM mes_products WHERE code = ?", (product_model,))
        if product is None:
            raise HTTPException(422, f"产品型号 {product_model} 不在产品主数据中，请先在 MES 页同步或本地添加")
        if not product["enabled"]:
            raise HTTPException(422, f"产品 {product_model} 已停用，不能绑定新流程")
    if station_code and database.one("SELECT 1 FROM mes_stations LIMIT 1"):
        station = database.one("SELECT enabled FROM mes_stations WHERE code = ?", (station_code,))
        if station is None:
            raise HTTPException(422, f"工站 {station_code} 不在工站主数据中，请先在 MES 页同步或本地添加")
        if not station["enabled"]:
            raise HTTPException(422, f"工站 {station_code} 已停用，不能绑定新流程")
    if station_code:
        conflict = database.one(
            "SELECT id, name FROM scripts WHERE product_model = ? AND station_code = ? "
            "AND enabled = 1 AND id != ?",
            (product_model, station_code, exclude_id or -1),
        )
        if conflict:
            raise HTTPException(
                409, f"产品 {product_model} 在工站 {station_code} 下已有启用流程「{conflict['name']}」(ID {conflict['id']})"
            )
    return station_code


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


def _effective_station(station: str | None) -> str:
    """本次测试的执行工站：执行台选中的测试工站即执行工站，未指定时回退到工位策略配置的终端工站。"""
    value = (station or "").strip()
    return value or str(database.setting("station_id", DEFAULT_SETTINGS["station_id"]))


def _effective_max_retests(script: sqlite3.Row) -> int:
    """计算当前流程生效的失败复测次数上限。

    优先取流程定义中的 max_retests（流程级策略，可按产品差异化配置）；
    流程未配置时回退到工位策略中的全局 max_retests 设置。
    """
    definition = json.loads(script["definition_json"])
    value = definition.get("max_retests")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return int(database.setting("max_retests", DEFAULT_SETTINGS["max_retests"]))


def _run(run_id: str) -> dict[str, Any]:
    row = database.one("SELECT * FROM test_runs WHERE id = ?", (run_id,))
    if row is None:
        raise HTTPException(404, "测试运行记录不存在")
    items = database.all("SELECT * FROM test_items WHERE run_id = ? ORDER BY sequence", (run_id,))
    report = database.one("SELECT id, report_no, mes_status FROM reports WHERE run_id = ?", (run_id,))
    controls = database.all("SELECT * FROM control_events WHERE run_id = ? ORDER BY id", (run_id,))
    flash_records = database.all("SELECT * FROM flash_records WHERE run_id = ? ORDER BY id", (run_id,))
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
                "node_id": item["node_id"] or item["item_id"],
                "path": item["path"] or item["item_id"],
                "iteration": item["iteration"],
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
        "controls": [
            {
                "node_id": control["node_id"],
                "name": control["node_name"],
                "type": control["node_type"],
                "path": control["path"],
                "status": control["status"],
                "details": json.loads(control["details_json"]),
                "started_at": control["started_at"],
                "finished_at": control["finished_at"],
            }
            for control in controls
        ],
        "flash_records": [
            {
                "item_path": record["item_path"],
                "sn": record["sn"],
                "firmware_name": record["firmware_name"],
                "firmware_version": record["firmware_version"],
                "backend": record["backend"],
                "ok": bool(record["ok"]),
                "message": record["message"],
                "duration_ms": record["duration_ms"],
                "finished_at": record["finished_at"],
            }
            for record in flash_records
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
    master = config.get("master_data")
    if master is None:
        return
    if not isinstance(master, dict):
        raise HTTPException(422, "master_data 必须是 JSON 对象")
    if str(master.get("mode", "simulator")) not in {"simulator", "http"}:
        raise HTTPException(422, "master_data.mode 仅支持 simulator 或 http")
    for entity in ("products", "stations"):
        entity_config = master.get(entity)
        if entity_config is None:
            continue
        if not isinstance(entity_config, dict):
            raise HTTPException(422, f"master_data.{entity} 必须是 JSON 对象")
        url = str(entity_config.get("url", "")).strip()
        if master.get("mode") == "http" and not url.startswith(("http://", "https://")):
            raise HTTPException(422, f"master_data.{entity}.url 必须是 HTTP/HTTPS URL")
        if str(entity_config.get("method", "GET")).upper() not in {"GET", "POST"}:
            raise HTTPException(422, f"master_data.{entity}.method 仅支持 GET 或 POST")
        if not isinstance(entity_config.get("headers", {}), dict):
            raise HTTPException(422, f"master_data.{entity}.headers 必须是 JSON 对象")
        if not isinstance(entity_config.get("field_map", {}), dict):
            raise HTTPException(422, f"master_data.{entity}.field_map 必须是 JSON 对象")
        if not isinstance(entity_config.get("items_path", ""), str):
            raise HTTPException(422, f"master_data.{entity}.items_path 必须是字符串")


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
    station_code = _validate_script_binding(definition)
    now = utc_now()
    cursor = None
    with database.transaction() as connection:
        cursor = connection.execute(
            "INSERT INTO scripts(name, product_model, station_code, definition_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (definition["name"], definition["product_model"], station_code, json_dumps(definition), now, now),
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
    station_code = _validate_script_binding(definition, exclude_id=script_id)
    database.execute(
        "UPDATE scripts SET name = ?, product_model = ?, station_code = ?, version = version + 1, enabled = 1, "
        "definition_json = ?, updated_at = ? WHERE id = ?",
        (
            definition["name"],
            definition["product_model"],
            station_code,
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
    try:
        validate_codec_config(payload.config)
    except DataCodecError as exc:
        raise HTTPException(422, str(exc)) from exc
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
    try:
        validate_codec_config(payload.config)
    except DataCodecError as exc:
        raise HTTPException(422, str(exc)) from exc
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


@app.get("/api/firmware")
def list_firmware() -> list[dict[str, Any]]:
    return load_firmware_index()


@app.post("/api/firmware", status_code=201)
def register_firmware_api(payload: FirmwareRegisterPayload) -> dict[str, Any]:
    try:
        return register_firmware(payload.source, payload.version, payload.product_model, payload.description)
    except FlashError as exc:
        raise HTTPException(422, str(exc)) from exc


MAX_FIRMWARE_UPLOAD_BYTES = 256 * 1024 * 1024


@app.post("/api/firmware/upload", status_code=201)
async def upload_firmware_api(
    request: Request,
    name: str = Query(min_length=1, max_length=255),
    version: str = Query(min_length=1, max_length=64),
    product_model: str = Query(default="", max_length=64),
    description: str = Query(default="", max_length=500),
) -> dict[str, Any]:
    """浏览器文件选择上传：请求体为固件原始字节，元信息走查询参数。"""
    filename = Path(name.replace("\\", "/")).name
    if not filename:
        raise HTTPException(422, "固件文件名不能为空")
    data = await request.body()
    if not data:
        raise HTTPException(422, "固件文件内容为空")
    if len(data) > MAX_FIRMWARE_UPLOAD_BYTES:
        raise HTTPException(422, "固件文件超过 256 MB 上限")
    with tempfile.TemporaryDirectory() as staging:
        staged = Path(staging) / filename
        staged.write_bytes(data)
        try:
            return register_firmware(staged, version, product_model, description)
        except FlashError as exc:
            raise HTTPException(422, str(exc)) from exc


@app.post("/api/firmware/{name}/verify")
def verify_firmware(name: str) -> dict[str, Any]:
    try:
        entry = find_firmware(name)
    except FlashError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True, "message": f"固件 {entry['name']} 校验通过，SHA-256 与登记一致", "firmware": entry}


@app.get("/api/flashers")
def get_flashers() -> list[dict[str, Any]]:
    return flasher_catalog()


@app.get("/api/flasher/config")
def get_flasher_config() -> dict[str, Any]:
    return database.setting("flasher", DEFAULT_FLASHER_CONFIG)


@app.put("/api/flasher/config")
def save_flasher_config(payload: FlasherConfigPayload) -> dict[str, Any]:
    if payload.backend not in FLASHER_CLASSES:
        raise HTTPException(422, f"不支持的烧录后端：{payload.backend}")
    config = {"backend": payload.backend, "config": payload.config}
    database.save_setting("flasher", config)
    return config


@app.post("/api/flasher/test")
def test_flasher(payload: FlasherConfigPayload) -> dict[str, Any]:
    if payload.backend not in FLASHER_CLASSES:
        raise HTTPException(422, f"不支持的烧录后端：{payload.backend}")
    flasher = create_flasher(payload.backend, payload.config)
    try:
        return flasher.probe()
    finally:
        try:
            flasher.close()
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


def _master_error(exc: MESMasterError) -> HTTPException:
    if isinstance(exc, MESSyncError):
        return HTTPException(502, str(exc))
    if isinstance(exc, MESMasterMissingError):
        return HTTPException(404, str(exc))
    if isinstance(exc, MESMasterConflictError):
        return HTTPException(409, str(exc))
    return HTTPException(422, str(exc))


@app.get("/api/mes/products")
def list_master_products(include_disabled: bool = True) -> dict[str, Any]:
    return MESMasterData(database).list_items("products", include_disabled)


@app.get("/api/mes/stations")
def list_master_stations(include_disabled: bool = True) -> dict[str, Any]:
    return MESMasterData(database).list_items("stations", include_disabled)


@app.post("/api/mes/sync")
def sync_master_data(payload: MasterSyncPayload) -> dict[str, Any]:
    try:
        return MESMasterData(database).sync(payload.target)
    except MESMasterError as exc:
        raise _master_error(exc) from exc


@app.post("/api/mes/products", status_code=201)
def create_master_product(payload: MasterItemPayload) -> dict[str, Any]:
    try:
        item = MESMasterData(database).create_item("products", payload.model_dump())
    except MESMasterError as exc:
        raise _master_error(exc) from exc
    return {"message": "本地产品已添加", "item": item}


@app.put("/api/mes/products/{code}")
def update_master_product(code: str, payload: MasterItemPayload) -> dict[str, Any]:
    try:
        item = MESMasterData(database).update_item("products", code, payload.model_dump())
    except MESMasterError as exc:
        raise _master_error(exc) from exc
    return {"message": "产品已更新", "item": item}


@app.delete("/api/mes/products/{code}")
def delete_master_product(code: str) -> dict[str, str]:
    try:
        MESMasterData(database).delete_item("products", code)
    except MESMasterError as exc:
        raise _master_error(exc) from exc
    return {"message": "本地产品已删除"}


@app.post("/api/mes/stations", status_code=201)
def create_master_station(payload: MasterItemPayload) -> dict[str, Any]:
    try:
        item = MESMasterData(database).create_item("stations", payload.model_dump())
    except MESMasterError as exc:
        raise _master_error(exc) from exc
    return {"message": "本地工站已添加", "item": item}


@app.put("/api/mes/stations/{code}")
def update_master_station(code: str, payload: MasterItemPayload) -> dict[str, Any]:
    try:
        item = MESMasterData(database).update_item("stations", code, payload.model_dump())
    except MESMasterError as exc:
        raise _master_error(exc) from exc
    return {"message": "工站已更新", "item": item}


@app.delete("/api/mes/stations/{code}")
def delete_master_station(code: str) -> dict[str, str]:
    try:
        MESMasterData(database).delete_item("stations", code)
    except MESMasterError as exc:
        raise _master_error(exc) from exc
    return {"message": "本地工站已删除"}


@app.post("/api/test-runs/scan")
def scan_sn(payload: SNPayload) -> dict[str, Any]:
    """扫码预检：判定该 SN 是否允许（复）测。

    判定规则（对所有测试流程统一生效）：
    1. 已存在 PASS 报告 → 禁止重复测试；
    2. 已完成次数（按 SN 跨全部流程累计 test_runs 中的 PASS/FAIL）达到
       1 + 生效复测上限 → 禁止再测；失败数据全部保留，不会因复测被清除；
    3. 本工位有正在执行的测试 → 提示等待。
    """
    sn = _validate_sn(payload.sn)
    script = _get_script(payload.script_id)
    now = utc_now()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO products(sn, product_model, first_seen_at) VALUES (?, ?, ?) "
            "ON CONFLICT(sn) DO UPDATE SET product_model = excluded.product_model",
            (sn, script["product_model"], now),
        )
    passed = database.one(
        "SELECT id, report_no FROM reports WHERE sn = ? AND status = 'PASS' LIMIT 1", (sn,)
    )
    # 最新一份报告仅用于前端展示“查看已有报告”，不再作为能否复测的依据
    latest_report = database.one(
        "SELECT id, report_no, status FROM reports WHERE sn = ? "
        "ORDER BY finished_at DESC, rowid DESC LIMIT 1",
        (sn,),
    )
    active = database.one(
        "SELECT id, sn FROM test_runs WHERE status = 'RUNNING' AND station = ?",
        (_effective_station(payload.station),),
    )
    max_retests = _effective_max_retests(script)
    max_attempts = 1 + max_retests
    # 复测计数按 SN 跨全部测试流程累计：换流程、换脚本版本都不能绕过失败次数限制
    attempts = database.one(
        "SELECT COUNT(*) AS count FROM test_runs WHERE sn = ? AND status IN ('PASS', 'FAIL')", (sn,)
    )["count"]
    result = {
        "sn": sn,
        "product_model": script["product_model"],
        "can_start": passed is None and attempts < max_attempts and active is None,
        "report": dict(latest_report) if latest_report else None,
        "active_run_id": active["id"] if active else None,
        "attempt": attempts + 1,
        "max_attempts": max_attempts,
        "message": "SN 校验通过，可以开始测试",
    }
    if passed is not None:
        result["message"] = "该 SN 已测试合格，禁止重复测试"
    elif attempts >= max_attempts:
        result["message"] = f"该 SN 复测次数已用完（已测 {attempts}/{max_attempts} 次），禁止重复测试"
    elif active:
        result["message"] = f"工位正在测试 {active['sn']}，请等待完成"
    elif attempts > 0:
        result["message"] = f"该 SN 上次测试失败，允许复测（第 {attempts + 1}/{max_attempts} 次）"
    return result


@app.post("/api/test-runs", status_code=201)
def start_test(payload: StartPayload, request: Request) -> dict[str, Any]:
    sn = _validate_sn(payload.sn)
    script = _get_script(payload.script_id)
    station = _effective_station(payload.station)
    operator = request.state.user["username"]
    script_station = (script["station_code"] or "").strip()
    if script_station and script_station != station:
        raise HTTPException(409, f"该流程绑定工站 {script_station}，与所选测试工站 {station} 不一致，禁止执行")
    run_id = new_id("run")
    started_at = utc_now()
    definition = json.loads(script["definition_json"])
    try:
        with database.transaction() as connection:
            # 与扫码预检相同的规则在事务内复检，防止并发请求绕过限制：
            # 1) 已有 PASS 报告禁止再测；2) 跨全部流程累计的失败/完成次数达到上限禁止再测。
            if connection.execute(
                "SELECT 1 FROM reports WHERE sn = ? AND status = 'PASS' LIMIT 1", (sn,)
            ).fetchone():
                raise HTTPException(409, "该 SN 已测试合格，禁止重复测试")
            max_attempts = 1 + _effective_max_retests(script)
            attempts = connection.execute(
                "SELECT COUNT(*) AS count FROM test_runs WHERE sn = ? AND status IN ('PASS', 'FAIL')", (sn,)
            ).fetchone()["count"]
            if attempts >= max_attempts:
                raise HTTPException(409, f"该 SN 复测次数已用完（已测 {attempts}/{max_attempts} 次），禁止重复测试")
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
            if definition.get("schema_version", 1) == 1:
                for sequence, step in enumerate(definition["steps"]):
                    connection.execute(
                        "INSERT INTO test_items(run_id, item_id, node_id, path, item_name, sequence, status) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'PENDING')",
                        (run_id, step["id"], step["id"], step["id"], step["name"], sequence),
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
    # no-cache：浏览器每次都回源校验首页，避免服务重启/前端更新后仍展示缓存的旧页面
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})
