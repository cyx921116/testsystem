"""FastAPI 应用：全部 HTTP API 与静态页面托管。

路由分组：
- /api/auth/*        登录、会话与账号管理（auth.py 提供路由，此处统一鉴权）
- /api/scripts*      测试流程的编译、保存、停用（保存即固化版本）
- /api/protocols*    通讯协议配置与连接测试
- /api/firmware*     固件仓登记/上传/校验；/api/flashers 烧录后端
- /api/settings      工位策略；/api/mes/* MES 配置、报文预览与主数据同步维护
- /api/test-runs*    扫码预检、开始测试、运行状态轮询（生产执行核心）
- /api/reports*      报告查询、下载、MES 上传与上传日志

鉴权模型：除健康检查与登录外的 /api 请求都经过 authenticate_request 中间件；
操作工只放行生产必需的只读接口与扫码/开始测试，其余写操作要求管理员。
"""
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
    """应用生命周期：启动时初始化数据库（建表/迁移/默认数据）并收尾中断的运行。"""
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
    """操作工权限白名单：生产必需的只读接口 + 扫码/开始测试；其余一律要求管理员。"""
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
    """认证与授权中间件：解析会话 Cookie，未登录 401，操作工越权 403。"""
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
    """流程保存/校验请求体：definition 为低代码流程 JSON（v1 步骤式或 v2 控制流）。"""

    definition: dict[str, Any]


class SNPayload(BaseModel):
    """扫码预检请求体：station 为执行台选中的测试工站（缺省回退终端工站）。"""

    sn: str = Field(min_length=1, max_length=128)
    script_id: int
    station: str | None = Field(default=None, max_length=64)


class StartPayload(SNPayload):
    """开始测试请求体；operator 字段仅为兼容保留，实际取登录用户名（防伪造）。"""

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
    """scripts 行 → API 输出：解析定义 JSON，附版本与启停状态。"""
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
    """取流程行；默认只取启用中的（停用流程不可再执行，历史报告不受影响）。"""
    row = database.one("SELECT * FROM scripts WHERE id = ?", (script_id,))
    if row is None or (enabled_only and not row["enabled"]):
        raise HTTPException(404, "测试脚本不存在或已停用")
    return row


def _validate_sn(sn: str) -> str:
    """按工位策略的 SN 正则做完整匹配校验；不合法返回 422，正则配置失效返回 500。"""
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
    """组装运行详情：运行状态 + 逐项结果 + 控制流轨迹 + 烧录记录 + 关联报告，供前端轮询。"""
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
    """reports 行 → 列表项输出（不含完整 report_json，详情走 /api/reports/{id}）。"""
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
    """保存 MES 配置前的整体校验：地址/方法/超时/报文模式/主数据段逐项检查。"""
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
    """健康检查（免登录）：服务状态、当前时间与终端工站。"""
    return {"status": "ok", "time": utc_now(), "station": database.setting("station_id")}


@app.get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    """生产页指标卡与最近报告：今日/累计测试、通过率、活动运行。"""
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
    """流程列表（启用在前、按更新时间倒序），执行台与流程库共用。"""
    return [_script(row) for row in database.all("SELECT * FROM scripts ORDER BY enabled DESC, updated_at DESC")]


@app.post("/api/scripts", status_code=201)
def create_script(payload: ScriptPayload) -> dict[str, Any]:
    """新建流程：先编译校验，再校验产品/工站绑定（主数据非空时必须在主数据内）。"""
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
    """仅编译校验不保存：返回是否通过、错误列表或规范化后的定义。"""
    try:
        compiled = compile_script(payload.definition)
    except ScriptValidationError as exc:
        return {"valid": False, "errors": exc.errors}
    return {"valid": True, "message": f"校验通过，共 {len(compiled['steps'])} 个测试项", "compiled": compiled}


@app.put("/api/scripts/{script_id}")
def update_script(script_id: int, payload: ScriptPayload) -> dict[str, Any]:
    """更新流程：版本号 +1 并重新启用；历史报告仍引用各自的脚本快照。"""
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
    """停用流程（软删除）：执行台不再索引，历史报告保留快照。"""
    _get_script(script_id)
    database.execute("UPDATE scripts SET enabled = 0, updated_at = ? WHERE id = ?", (utc_now(), script_id))
    return {"message": "脚本已停用，历史报告仍保留原脚本快照"}


@app.get("/api/protocols")
def get_protocols() -> list[dict[str, Any]]:
    """协议目录（字段定义 + 驱动可用性）并附上已保存的连接配置。"""
    configs = {row["name"]: json.loads(row["config_json"]) for row in database.all("SELECT * FROM protocol_configs")}
    return [{**item, "config": configs.get(item["name"], {})} for item in protocol_catalog()]


@app.put("/api/protocols/{name}")
def save_protocol(name: str, payload: ConfigPayload) -> dict[str, Any]:
    """保存协议连接配置（先过编解码预检）。"""
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
    """测试连接：用请求体中的配置即时建立连接验证（不要求先保存）。"""
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
    """固件仓清单（含版本、适用型号、SHA-256 校验和）。"""
    return load_firmware_index()


@app.post("/api/firmware", status_code=201)
def register_firmware_api(payload: FirmwareRegisterPayload) -> dict[str, Any]:
    """按工作站本地路径登记固件入仓（复制进固件仓并记录校验和）。"""
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
    """校验固件：文件存在且 SHA-256 与登记一致（防烧入被替换的文件）。"""
    try:
        entry = find_firmware(name)
    except FlashError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True, "message": f"固件 {entry['name']} 校验通过，SHA-256 与登记一致", "firmware": entry}


@app.get("/api/flashers")
def get_flashers() -> list[dict[str, Any]]:
    """烧录后端目录（名称、描述、驱动可用性）。"""
    return flasher_catalog()


@app.get("/api/flasher/config")
def get_flasher_config() -> dict[str, Any]:
    """读取当前烧录后端配置。"""
    return database.setting("flasher", DEFAULT_FLASHER_CONFIG)


@app.put("/api/flasher/config")
def save_flasher_config(payload: FlasherConfigPayload) -> dict[str, Any]:
    """保存烧录后端配置（后端类型必须在注册表内）。"""
    if payload.backend not in FLASHER_CLASSES:
        raise HTTPException(422, f"不支持的烧录后端：{payload.backend}")
    config = {"backend": payload.backend, "config": payload.config}
    database.save_setting("flasher", config)
    return config


@app.post("/api/flasher/test")
def test_flasher(payload: FlasherConfigPayload) -> dict[str, Any]:
    """探测烧录后端可用性（驱动是否安装、命令模板是否配置等）。"""
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
    """读取工位策略（终端工站、默认操作员、SN 正则、自动上传、复测上限）。"""
    return {key: database.setting(key, value) for key, value in DEFAULT_SETTINGS.items()}


@app.put("/api/settings")
def save_settings(payload: SettingsPayload) -> dict[str, Any]:
    """保存工位策略（仅管理员）：SN 正则会先做编译校验。"""
    try:
        re.compile(payload.sn_pattern)
    except re.error as exc:
        raise HTTPException(422, f"SN 正则表达式无效：{exc}") from exc
    for key, value in payload.model_dump().items():
        database.save_setting(key, value.strip() if isinstance(value, str) else value)
    return get_settings()


@app.get("/api/mes/config")
def get_mes_config() -> dict[str, Any]:
    """读取 MES 配置（报告上传 + master_data 主数据段）。"""
    return database.setting("mes", DEFAULT_MES_CONFIG)


@app.put("/api/mes/config")
def save_mes_config(payload: ConfigPayload) -> dict[str, Any]:
    """保存 MES 配置（先整体校验，含 master_data 段）。"""
    _validate_mes_config(payload.config)
    database.save_setting("mes", payload.config)
    return payload.config


@app.get("/api/mes/preview")
def preview_mes_payload() -> dict[str, Any]:
    """报文预览：用最新报告（没有则用内置示例）渲染实际发送结构，不发起网络请求。"""
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
    """主数据异常 → HTTP 状态码映射：同步失败 502、不存在 404、冲突 409、参数 422。"""
    if isinstance(exc, MESSyncError):
        return HTTPException(502, str(exc))
    if isinstance(exc, MESMasterMissingError):
        return HTTPException(404, str(exc))
    if isinstance(exc, MESMasterConflictError):
        return HTTPException(409, str(exc))
    return HTTPException(422, str(exc))


@app.get("/api/mes/products")
def list_master_products(include_disabled: bool = True) -> dict[str, Any]:
    """产品主数据列表（操作工只读，执行台/流程编排的产品下拉数据源）。"""
    return MESMasterData(database).list_items("products", include_disabled)


@app.get("/api/mes/stations")
def list_master_stations(include_disabled: bool = True) -> dict[str, Any]:
    """工站主数据列表（操作工只读，执行台工站与工位策略下拉数据源）。"""
    return MESMasterData(database).list_items("stations", include_disabled)


@app.post("/api/mes/sync")
def sync_master_data(payload: MasterSyncPayload) -> dict[str, Any]:
    """手动同步主数据（管理员）：从 MES/模拟器拉取并 upsert，返回新增/更新/停用统计。"""
    try:
        return MESMasterData(database).sync(payload.target)
    except MESMasterError as exc:
        raise _master_error(exc) from exc


@app.post("/api/mes/products", status_code=201)
def create_master_product(payload: MasterItemPayload) -> dict[str, Any]:
    """添加本地产品（管理员）；编号冲突返回 409。"""
    try:
        item = MESMasterData(database).create_item("products", payload.model_dump())
    except MESMasterError as exc:
        raise _master_error(exc) from exc
    return {"message": "本地产品已添加", "item": item}


@app.put("/api/mes/products/{code}")
def update_master_product(code: str, payload: MasterItemPayload) -> dict[str, Any]:
    """更新产品：本地行可改名称/规格，MES 行仅支持启用/停用。"""
    try:
        item = MESMasterData(database).update_item("products", code, payload.model_dump())
    except MESMasterError as exc:
        raise _master_error(exc) from exc
    return {"message": "产品已更新", "item": item}


@app.delete("/api/mes/products/{code}")
def delete_master_product(code: str) -> dict[str, str]:
    """删除本地产品；MES 同步行返回 409。"""
    try:
        MESMasterData(database).delete_item("products", code)
    except MESMasterError as exc:
        raise _master_error(exc) from exc
    return {"message": "本地产品已删除"}


@app.post("/api/mes/stations", status_code=201)
def create_master_station(payload: MasterItemPayload) -> dict[str, Any]:
    """添加本地工站（管理员）；编号冲突返回 409。"""
    try:
        item = MESMasterData(database).create_item("stations", payload.model_dump())
    except MESMasterError as exc:
        raise _master_error(exc) from exc
    return {"message": "本地工站已添加", "item": item}


@app.put("/api/mes/stations/{code}")
def update_master_station(code: str, payload: MasterItemPayload) -> dict[str, Any]:
    """更新工站：本地行可改名称/产线/类型，MES 行仅支持启用/停用。"""
    try:
        item = MESMasterData(database).update_item("stations", code, payload.model_dump())
    except MESMasterError as exc:
        raise _master_error(exc) from exc
    return {"message": "工站已更新", "item": item}


@app.delete("/api/mes/stations/{code}")
def delete_master_station(code: str) -> dict[str, str]:
    """删除本地工站；MES 同步行返回 409。"""
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
    """开始测试：在事务内复检所有限制后创建 RUNNING 运行并提交后台执行器。

    执行工站取执行台选中的测试工站（缺省回退终端工站）；流程绑定工站与所选
    工站不一致返回 409。操作员取登录用户名，忽略客户端伪造的 operator 字段。
    """
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
    """当前活动运行（页面刷新后恢复执行台状态用）；无则返回 null。"""
    row = database.one("SELECT id FROM test_runs WHERE status = 'RUNNING' ORDER BY started_at DESC LIMIT 1")
    return _run(row["id"]) if row else None


@app.get("/api/test-runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    """运行详情（状态 + 逐项结果 + 控制流 + 烧录记录），前端执行中每 450ms 轮询一次。"""
    return _run(run_id)


@app.get("/api/reports")
def list_reports(
    sn: str = "",
    status: str = "",
    mes_status: str = "",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """报告列表：SN 模糊搜索 + 结果/MES 状态筛选，按完成时间倒序分页。"""
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
    """报告详情：完整报告 JSON（含脚本快照、测试项、控制流、烧录记录）+ MES 状态。"""
    row = database.one("SELECT * FROM reports WHERE id = ?", (report_id,))
    if row is None:
        raise HTTPException(404, "报告不存在")
    report = json.loads(row["report_json"])
    report.update(_report_list_item(row))
    return report


@app.get("/api/reports/{report_id}/download")
def download_report(report_id: str) -> JSONResponse:
    """下载报告 JSON 文件（文件名即报告编号）。"""
    row = database.one("SELECT report_no, report_json FROM reports WHERE id = ?", (report_id,))
    if row is None:
        raise HTTPException(404, "报告不存在")
    return JSONResponse(
        content=json.loads(row["report_json"]),
        headers={"Content-Disposition": f'attachment; filename="{row["report_no"]}.json"'},
    )


@app.get("/api/reports/{report_id}/upload-logs")
def get_upload_logs(report_id: str) -> list[dict[str, Any]]:
    """报告的 MES 上传日志（每次尝试的请求与响应留痕）。"""
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
    """手动上传/重试上传报告到 MES（管理员）；失败返回 502 并保留响应原文。"""
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
