"""SQLite 数据访问层：表结构定义、初始化/轻量迁移、事务与常用查询封装。

设计要点：
- 单文件 SQLite（WAL 模式），随 Python 自带，适合单工位工作站离线运行；
- 所有表在 ``SCHEMA`` 中 ``CREATE TABLE IF NOT EXISTS`` 定义，``initialize()`` 启动时执行，
  并对老库做列级迁移（ALTER TABLE 补列、reports 表重建等），保证老数据文件可直接升级；
- 唯一索引兜底关键业务规则：SN 报告幂等、单工站活动运行唯一、控制流路径唯一；
- 默认配置（协议、工位策略、MES、烧录后端）在初始化时写入 settings 表，可页面化修改。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .compiler import DEFAULT_SCRIPT, compile_script
from .protocol_definitions import DEFAULT_PROTOCOL_CONFIGS
from .utils import json_dumps, utc_now


# 全量建表脚本。各表用途：
#   products        SN 登记表：证明“先扫码后测试”，记录 SN 与产品型号的绑定
#   scripts         测试流程（低代码 JSON 定义，含版本与启停；station_code 为绑定工站）
#   mes_products / mes_stations  MES 主数据本地缓存（产品/工站，source 区分 MES 同步与本地添加）
#   protocol_configs 通讯协议连接配置（按协议名一行）
#   settings        键值配置（工位策略、MES、烧录后端、登录密钥等，value 为 JSON）
#   users           登录账号（角色、加盐口令哈希、会话版本号）
#   test_runs       测试运行（一次扫码测试一条，含脚本快照，保证报告可复现）
#   test_items      运行内的逐项结果（含控制流展开后的节点路径与迭代次数）
#   control_events  条件/循环/并行/子流程等控制节点的执行轨迹
#   reports         正式报告（每次测试一份，含完整报告 JSON 与 MES 上传状态）
#   mes_upload_logs MES 上传日志（每次尝试一条，含请求与响应原文）
#   flash_records   固件烧录记录（固件、校验和、后端、结果）
SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sn TEXT PRIMARY KEY,
    product_model TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_tested_at TEXT
);

CREATE TABLE IF NOT EXISTS scripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    product_model TEXT NOT NULL,
    station_code TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mes_products (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    spec TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'local' CHECK(source IN ('mes', 'local')),
    enabled INTEGER NOT NULL DEFAULT 1,
    raw_json TEXT NOT NULL DEFAULT '{}',
    synced_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mes_stations (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    line TEXT NOT NULL DEFAULT '',
    station_type TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'local' CHECK(source IN ('mes', 'local')),
    enabled INTEGER NOT NULL DEFAULT 1,
    raw_json TEXT NOT NULL DEFAULT '{}',
    synced_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS protocol_configs (
    name TEXT PRIMARY KEY,
    config_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'operator')),
    password_salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_role ON users(role, enabled);

CREATE TABLE IF NOT EXISTS test_runs (
    id TEXT PRIMARY KEY,
    sn TEXT NOT NULL,
    product_model TEXT NOT NULL,
    script_id INTEGER NOT NULL,
    script_version INTEGER NOT NULL,
    script_snapshot_json TEXT NOT NULL,
    station TEXT NOT NULL,
    operator TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('RUNNING', 'PASS', 'FAIL')),
    current_item TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY(sn) REFERENCES products(sn),
    FOREIGN KEY(script_id) REFERENCES scripts(id)
);

-- 同一工站最多一条 RUNNING：数据库层防止两个产品同时争用同一套硬件
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_station
ON test_runs(station) WHERE status = 'RUNNING';
CREATE INDEX IF NOT EXISTS idx_runs_sn ON test_runs(sn);
CREATE INDEX IF NOT EXISTS idx_runs_started ON test_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS test_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    node_id TEXT,
    path TEXT,
    iteration INTEGER,
    item_name TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'RUNNING', 'PASS', 'FAIL')),
    expected TEXT,
    actual TEXT,
    message TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    details_json TEXT NOT NULL DEFAULT '[]',
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(run_id) REFERENCES test_runs(id) ON DELETE CASCADE,
    UNIQUE(run_id, item_id)
);

CREATE TABLE IF NOT EXISTS control_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    node_name TEXT NOT NULL,
    node_type TEXT NOT NULL,
    path TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY(run_id) REFERENCES test_runs(id) ON DELETE CASCADE,
    UNIQUE(run_id, path)
);

CREATE INDEX IF NOT EXISTS idx_control_events_run ON control_events(run_id, id);

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    report_no TEXT NOT NULL UNIQUE,
    -- 一个 SN 允许保留多份报告：每次测试（含每次失败复测）各存一份，失败数据永不清除
    sn TEXT NOT NULL,
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
    uploaded_at TEXT,
    FOREIGN KEY(sn) REFERENCES products(sn),
    FOREIGN KEY(run_id) REFERENCES test_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_reports_finished ON reports(finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_mes ON reports(mes_status);
CREATE INDEX IF NOT EXISTS idx_reports_sn ON reports(sn);

CREATE TABLE IF NOT EXISTS mes_upload_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    request_json TEXT NOT NULL,
    success INTEGER NOT NULL,
    response TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(report_id) REFERENCES reports(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS flash_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    item_path TEXT,
    sn TEXT NOT NULL,
    station TEXT,
    operator TEXT,
    firmware_name TEXT,
    firmware_version TEXT,
    firmware_sha256 TEXT,
    backend TEXT,
    ok INTEGER NOT NULL,
    message TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    details_json TEXT NOT NULL DEFAULT '{}',
    finished_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES test_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_flash_records_run ON flash_records(run_id, id);
CREATE INDEX IF NOT EXISTS idx_flash_records_sn ON flash_records(sn, id);
"""

# MES 默认配置：报告上传（endpoint/映射模板）+ 主数据同步（master_data 段）。
# 模拟器模式使用内置示例主数据；HTTP 模式按 url/items_path/field_map 拉取真实 MES。
DEFAULT_MES_CONFIG = {
    "endpoint": "http://127.0.0.1:9000/api/mes/test-reports",
    "method": "POST",
    "timeout_seconds": 10,
    "headers": {"Content-Type": "application/json"},
    "idempotency_header": "Idempotency-Key",
    "payload_mode": "mapped",
    "payload_mapping": {
        "event": "production_test.completed",
        "idempotencyKey": "${report_no}",
        "data": "${report}",
    },
    "master_data": {
        "mode": "simulator",
        "products": {
            "url": "",
            "method": "GET",
            "headers": {},
            "items_path": "data.list",
            "field_map": {"code": "itemCode", "name": "itemName", "spec": "specification"},
            "enabled": True,
        },
        "stations": {
            "url": "",
            "method": "GET",
            "headers": {},
            "items_path": "data.list",
            "field_map": {
                "code": "stationCode",
                "name": "stationName",
                "line": "lineCode",
                "station_type": "stationType",
            },
            "enabled": True,
        },
    },
}

# 工位策略默认值：终端工站、默认操作员、SN 正则、报告自动上传开关、失败复测上限
DEFAULT_SETTINGS = {
    "station_id": "STATION-01",
    "operator": "OP-001",
    "sn_pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{3,63}$",
    "auto_upload": False,
    "max_retests": 3,
}

# 烧录后端默认配置：仿真器后端（无硬件时可完整演示烧录流程）
DEFAULT_FLASHER_CONFIG = {
    "backend": "simulator",
    "config": {"step_delay_ms": 50},
}


# 旧版本 reports.sn 带 UNIQUE 约束（一个 SN 只保留一份报告，复测会覆盖删除失败报告）。
# 现要求失败数据全部留档，每次测试各存一份报告，因此需要重建表去掉该约束。
_REPORTS_REBUILD_SCRIPT = """
BEGIN IMMEDIATE;
CREATE TABLE reports_rebuilt (
    id TEXT PRIMARY KEY,
    report_no TEXT NOT NULL UNIQUE,
    sn TEXT NOT NULL,
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
    uploaded_at TEXT,
    FOREIGN KEY(sn) REFERENCES products(sn),
    FOREIGN KEY(run_id) REFERENCES test_runs(id)
);
INSERT INTO reports_rebuilt
    SELECT id, report_no, sn, run_id, product_model, station, operator, script_name, script_version,
           status, summary_json, report_json, started_at, finished_at, duration_ms,
           mes_status, mes_attempts, mes_response, uploaded_at
    FROM reports;
DROP TABLE reports;
ALTER TABLE reports_rebuilt RENAME TO reports;
CREATE INDEX IF NOT EXISTS idx_reports_finished ON reports(finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_mes ON reports(mes_status);
CREATE INDEX IF NOT EXISTS idx_reports_sn ON reports(sn);
COMMIT;
"""


def _reports_sn_is_unique(connection: sqlite3.Connection) -> bool:
    """检测 reports.sn 是否仍带旧版唯一索引（逐列比对，兼容 sqlite_autoindex 命名）。"""
    for index in connection.execute("PRAGMA index_list(reports)").fetchall():
        if not index["unique"]:
            continue
        columns = [
            row["name"]
            for row in connection.execute(f"PRAGMA index_info({index['name']})").fetchall()
        ]
        if columns == ["sn"]:
            return True
    return False


class Database:
    """SQLite 访问入口：负责连接管理、初始化迁移、事务与 settings 读写。

    每个操作使用独立短连接（sqlite3 连接非线程安全，执行器在线程池中运行），
    WAL 模式保证读写不互斥；写事务统一走 ``BEGIN IMMEDIATE`` 提前拿写锁，
    避免并发请求在提交时才冲突。
    """

    def __init__(self, path: str | Path | None = None):
        # 未显式指定路径时，使用 <工作目录>/data/testsystem.db（可用 TESTSYSTEM_DATA_DIR 覆盖）
        if path is None:
            data_dir = Path(os.getenv("TESTSYSTEM_DATA_DIR", str(Path.cwd() / "data")))
            data_dir.mkdir(parents=True, exist_ok=True)
            path = data_dir / "testsystem.db"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        """新建连接：Row 工厂按列名取值，开启外键约束与 15s 忙等待（配合 WAL）。"""
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        """普通会话：自动提交/回滚并关闭连接，供只读或单语句写入使用。"""
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """建表 + 轻量迁移 + 写入默认配置与演示流程；服务启动时调用一次。

        迁移内容：reports 表重建（去掉 SN 唯一约束）、test_items/scripts 补列、
        协议配置与 MES 配置按“默认值 + 已存值”合并（老配置自动获得新增字段）。
        """
        with self.session() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            if _reports_sn_is_unique(connection):
                # 重建 reports 表前关闭外键检查，避免 DROP 父表时级联清空 mes_upload_logs；
                # PRAGMA foreign_keys 仅在事务外生效，重建脚本自身用 BEGIN/COMMIT 保证原子性。
                connection.execute("PRAGMA foreign_keys = OFF")
                try:
                    connection.executescript(_REPORTS_REBUILD_SCRIPT)
                finally:
                    connection.execute("PRAGMA foreign_keys = ON")
            # test_items 为控制流版本补 node_id/path/iteration 列，并用 item_id 回填历史数据
            item_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(test_items)").fetchall()
            }
            for name, definition in {"node_id": "TEXT", "path": "TEXT", "iteration": "INTEGER"}.items():
                if name not in item_columns:
                    connection.execute(f"ALTER TABLE test_items ADD COLUMN {name} {definition}")
            connection.execute(
                "UPDATE test_items SET node_id = COALESCE(node_id, item_id), path = COALESCE(path, item_id)"
            )
            # scripts 为工站绑定补 station_code 列
            script_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(scripts)").fetchall()
            }
            if "station_code" not in script_columns:
                connection.execute("ALTER TABLE scripts ADD COLUMN station_code TEXT NOT NULL DEFAULT ''")
            now = utc_now()
            # 协议配置：缺失的协议补默认配置；已有配置与默认值合并（新增字段自动获得默认值），
            # 并兼容旧字段名 response_format → receive_format
            for name, config in DEFAULT_PROTOCOL_CONFIGS.items():
                connection.execute(
                    "INSERT OR IGNORE INTO protocol_configs(name, config_json, updated_at) VALUES (?, ?, ?)",
                    (name, json_dumps(config), now),
                )
                row = connection.execute("SELECT config_json FROM protocol_configs WHERE name = ?", (name,)).fetchone()
                stored = json.loads(row["config_json"])
                merged = {**config, **stored}
                if "receive_format" not in stored and "response_format" in stored:
                    merged["receive_format"] = stored["response_format"]
                if merged != stored:
                    connection.execute(
                        "UPDATE protocol_configs SET config_json = ? WHERE name = ?",
                        (json_dumps(merged), name),
                    )
            # 键值配置：工位策略、MES、烧录后端，缺失时写默认值
            for key, value in {**DEFAULT_SETTINGS, "mes": DEFAULT_MES_CONFIG, "flasher": DEFAULT_FLASHER_CONFIG}.items():
                connection.execute(
                    "INSERT OR IGNORE INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)",
                    (key, json_dumps(value), now),
                )
            # MES 配置合并：老配置缺 master_data 段或段内字段时按默认值补齐
            stored_mes = json.loads(
                connection.execute("SELECT value_json FROM settings WHERE key = 'mes'").fetchone()["value_json"]
            )
            merged_mes = {**DEFAULT_MES_CONFIG, **stored_mes}
            stored_master = stored_mes.get("master_data") if isinstance(stored_mes.get("master_data"), dict) else {}
            merged_master = {**DEFAULT_MES_CONFIG["master_data"], **stored_master}
            for entity in ("products", "stations"):
                stored_entity = stored_master.get(entity) if isinstance(stored_master.get(entity), dict) else {}
                merged_master[entity] = {**DEFAULT_MES_CONFIG["master_data"][entity], **stored_entity}
            merged_mes["master_data"] = merged_master
            if merged_mes != stored_mes:
                connection.execute(
                    "UPDATE settings SET value_json = ?, updated_at = ? WHERE key = 'mes'",
                    (json_dumps(merged_mes), now),
                )
            # 空库时写入演示流程，保证首次启动即可完整走通扫码测试
            if connection.execute("SELECT COUNT(*) FROM scripts").fetchone()[0] == 0:
                definition = compile_script(DEFAULT_SCRIPT)
                connection.execute(
                    "INSERT INTO scripts(name, product_model, definition_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (definition["name"], definition["product_model"], json_dumps(definition), now, now),
                )
        from .auth import AuthService

        # 初始化账号体系（默认 admin/operator 账号与登录密钥）；局部导入避免循环依赖
        AuthService(self).initialize()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：BEGIN IMMEDIATE 提前获取写锁，异常回滚，保证多步写入原子性。"""
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        """查询单行；无结果返回 None。"""
        with self.session() as connection:
            return connection.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """查询多行，返回 Row 列表（可按列名访问）。"""
        with self.session() as connection:
            return connection.execute(sql, params).fetchall()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """执行单条写语句，返回受影响行数。"""
        with self.session() as connection:
            cursor = connection.execute(sql, params)
            return cursor.rowcount

    def setting(self, key: str, default: Any = None) -> Any:
        """读取 settings 表中的 JSON 配置；不存在时返回 default。"""
        row = self.one("SELECT value_json FROM settings WHERE key = ?", (key,))
        return json.loads(row["value_json"]) if row else default

    def save_setting(self, key: str, value: Any) -> None:
        """写入 settings 表（存在则覆盖），value 序列化为 JSON。"""
        self.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
            (key, json_dumps(value), utc_now()),
        )

    def recover_interrupted_runs(self) -> None:
        """启动时收尾中断的运行：服务重启把 RUNNING 运行与其未完成项标记为失败。

        保证“单工站活动运行唯一”索引不会被崩溃残留占住，页面上也能看到明确的中断原因。
        """
        now = utc_now()
        with self.transaction() as connection:
            running = connection.execute("SELECT id FROM test_runs WHERE status = 'RUNNING'").fetchall()
            for run in running:
                connection.execute(
                    "UPDATE test_items SET status = 'FAIL', message = ?, finished_at = ? "
                    "WHERE run_id = ? AND status IN ('PENDING', 'RUNNING')",
                    ("服务重启导致测试中断，可重新扫码测试", now, run["id"]),
                )
                connection.execute(
                    "UPDATE control_events SET status = 'INTERRUPTED', finished_at = ? "
                    "WHERE run_id = ? AND status = 'RUNNING'",
                    (now, run["id"]),
                )
                connection.execute(
                    "UPDATE test_runs SET status = 'FAIL', error = ?, finished_at = ? WHERE id = ?",
                    ("服务重启导致测试中断", now, run["id"]),
                )
