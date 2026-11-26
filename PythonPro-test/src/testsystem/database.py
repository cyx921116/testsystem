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
    uploaded_at TEXT,
    FOREIGN KEY(sn) REFERENCES products(sn),
    FOREIGN KEY(run_id) REFERENCES test_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_reports_finished ON reports(finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_mes ON reports(mes_status);

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

DEFAULT_SETTINGS = {
    "station_id": "STATION-01",
    "operator": "OP-001",
    "sn_pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{3,63}$",
    "auto_upload": False,
    "max_retests": 3,
}

DEFAULT_FLASHER_CONFIG = {
    "backend": "simulator",
    "config": {"step_delay_ms": 50},
}


class Database:
    def __init__(self, path: str | Path | None = None):
        if path is None:
            data_dir = Path(os.getenv("TESTSYSTEM_DATA_DIR", str(Path.cwd() / "data")))
            data_dir.mkdir(parents=True, exist_ok=True)
            path = data_dir / "testsystem.db"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.session() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            item_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(test_items)").fetchall()
            }
            for name, definition in {"node_id": "TEXT", "path": "TEXT", "iteration": "INTEGER"}.items():
                if name not in item_columns:
                    connection.execute(f"ALTER TABLE test_items ADD COLUMN {name} {definition}")
            connection.execute(
                "UPDATE test_items SET node_id = COALESCE(node_id, item_id), path = COALESCE(path, item_id)"
            )
            script_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(scripts)").fetchall()
            }
            if "station_code" not in script_columns:
                connection.execute("ALTER TABLE scripts ADD COLUMN station_code TEXT NOT NULL DEFAULT ''")
            now = utc_now()
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
            for key, value in {**DEFAULT_SETTINGS, "mes": DEFAULT_MES_CONFIG, "flasher": DEFAULT_FLASHER_CONFIG}.items():
                connection.execute(
                    "INSERT OR IGNORE INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)",
                    (key, json_dumps(value), now),
                )
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
            if connection.execute("SELECT COUNT(*) FROM scripts").fetchone()[0] == 0:
                definition = compile_script(DEFAULT_SCRIPT)
                connection.execute(
                    "INSERT INTO scripts(name, product_model, definition_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (definition["name"], definition["product_model"], json_dumps(definition), now, now),
                )
        from .auth import AuthService

        AuthService(self).initialize()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
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
        with self.session() as connection:
            return connection.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.session() as connection:
            return connection.execute(sql, params).fetchall()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.session() as connection:
            cursor = connection.execute(sql, params)
            return cursor.rowcount

    def setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value_json FROM settings WHERE key = ?", (key,))
        return json.loads(row["value_json"]) if row else default

    def save_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
            (key, json_dumps(value), utc_now()),
        )

    def recover_interrupted_runs(self) -> None:
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
