from __future__ import annotations

import json
import sqlite3
import ssl
import urllib.error
import urllib.request
from typing import Any

from .database import Database
from .utils import json_dumps, resolve_path, utc_now


class MESMasterError(RuntimeError):
    """主数据配置或本地维护错误（参数、权限类）。"""


class MESMasterConflictError(MESMasterError):
    """编号冲突等本地维护冲突。"""


class MESMasterMissingError(MESMasterError):
    """编号不存在。"""


class MESSyncError(MESMasterError):
    """MES 端或网络导致的同步失败。"""


# 模拟器数据源：与真实 MES 查询返回保持同一套目标字段（code/name/...），
# 用于未接入 MES 时验证同步、绑定、权限等完整流程。
SIMULATOR_PRODUCTS = [
    {"code": "DEMO-100", "name": "仿真演示产品", "spec": "SIM-DEMO"},
    {"code": "SIM-200", "name": "仿真控制器", "spec": "SIM-2.0"},
    {"code": "SIM-300", "name": "仿真传感器", "spec": "SIM-3.0"},
]

SIMULATOR_STATIONS = [
    {"code": "STATION-01", "name": "1 号功能测试工站", "line": "L01", "station_type": "FUNCTIONAL"},
    {"code": "STATION-02", "name": "2 号功能测试工站", "line": "L01", "station_type": "FUNCTIONAL"},
    {"code": "ICT-01", "name": "ICT 测试工站", "line": "L02", "station_type": "ICT"},
]

# 每个实体允许的目标字段（首列为主键），同步映射和本地维护都以此为白名单。
ENTITY_FIELDS = {
    "products": ["code", "name", "spec"],
    "stations": ["code", "name", "line", "station_type"],
}


class MESMasterData:
    def __init__(self, database: Database):
        self.db = database

    def master_config(self) -> dict[str, Any]:
        mes = self.db.setting("mes", {}) or {}
        master = mes.get("master_data")
        return master if isinstance(master, dict) else {}

    @staticmethod
    def _require_entity(entity: str) -> None:
        if entity not in ENTITY_FIELDS:
            raise MESMasterError(f"未知的主数据类型：{entity}")

    # ------------------------------------------------------------------ 同步
    def sync(self, target: str) -> dict[str, Any]:
        if target not in {"products", "stations", "both"}:
            raise MESMasterError("同步目标必须是 products、stations 或 both")
        entities = ("products", "stations") if target == "both" else (target,)
        return {entity: self._sync_entity(entity) for entity in entities}

    def _sync_entity(self, entity: str) -> dict[str, Any]:
        config = self.master_config().get(entity) or {}
        if config.get("enabled", True) is False:
            raise MESMasterError(f"{entity} 同步未启用，请先在 MES 配置中开启")
        items = self._fetch_items(entity, config)
        now = utc_now()
        added = updated = disabled = 0
        seen: set[str] = set()
        fields = ENTITY_FIELDS[entity]
        assignments = ", ".join(f"{field} = ?" for field in fields[1:])
        with self.db.transaction() as connection:
            for item in items:
                code = str(item.get("code", "")).strip()
                if not code:
                    continue
                seen.add(code)
                values = [str(item.get(field, "")).strip()[:200] for field in fields[1:]]
                raw = item.get("raw")
                raw_json = json_dumps(raw if isinstance(raw, (dict, list)) else item)
                cursor = connection.execute(
                    f"UPDATE mes_{entity} SET {assignments}, source = 'mes', enabled = 1, "
                    "raw_json = ?, synced_at = ?, updated_at = ? WHERE code = ?",
                    (*values, raw_json, now, now, code),
                )
                if cursor.rowcount:
                    updated += 1
                    continue
                placeholders = ", ".join("?" * len(fields))
                connection.execute(
                    f"INSERT INTO mes_{entity}(code, {', '.join(fields[1:])}, source, enabled, raw_json, "
                    f"synced_at, created_at, updated_at) VALUES ({placeholders}, 'mes', 1, ?, ?, ?, ?)",
                    (code, *values, raw_json, now, now, now),
                )
                added += 1
            stale = connection.execute(
                f"SELECT code FROM mes_{entity} WHERE source = 'mes' AND enabled = 1"
            ).fetchall()
            for row in stale:
                if row["code"] not in seen:
                    disabled += 1
                    connection.execute(
                        f"UPDATE mes_{entity} SET enabled = 0, updated_at = ? WHERE code = ?",
                        (now, row["code"]),
                    )
        return {"total": len(seen), "added": added, "updated": updated, "disabled": disabled, "synced_at": now}

    def _fetch_items(self, entity: str, config: dict[str, Any]) -> list[dict[str, Any]]:
        mode = str(self.master_config().get("mode", "simulator")).strip().lower()
        if mode == "simulator":
            source = SIMULATOR_PRODUCTS if entity == "products" else SIMULATOR_STATIONS
            return [dict(item, raw=dict(item)) for item in source]
        if mode == "http":
            return self._fetch_http(entity, config)
        raise MESMasterError(f"未知的主数据模式：{mode}，仅支持 simulator 或 http")

    def _fetch_http(self, entity: str, config: dict[str, Any]) -> list[dict[str, Any]]:
        url = str(config.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            raise MESMasterError(f"MES {entity} 查询地址必须是 HTTP/HTTPS URL")
        method = str(config.get("method", "GET")).upper()
        if method not in {"GET", "POST"}:
            raise MESMasterError("主数据查询方法仅支持 GET 或 POST")
        headers = {str(key): str(value) for key, value in (config.get("headers") or {}).items()}
        headers.setdefault("Accept", "application/json")
        request = urllib.request.Request(url, headers=headers, method=method)
        ssl_context = None
        if url.startswith("https://") and self.master_config().get("verify_tls") is False:
            ssl_context = ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(
                request,
                timeout=float(config.get("timeout_seconds", 10) or 10),
                context=ssl_context,
            ) as response:
                body = json.loads(response.read(4 * 1024 * 1024).decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            text = exc.read(65536).decode("utf-8", errors="replace")
            raise MESSyncError(f"MES {entity} 查询返回 HTTP {exc.code}：{text[:500]}") from exc
        except urllib.error.URLError as exc:
            raise MESSyncError(f"MES {entity} 查询网络失败：{exc.reason}") from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise MESSyncError(f"MES {entity} 查询返回的不是合法 JSON") from exc
        items_path = str(config.get("items_path", "")).strip()
        try:
            container = resolve_path(body, items_path) if items_path else body
        except KeyError as exc:
            raise MESSyncError(f"MES {entity} 响应中找不到列表路径 {items_path}：{exc}") from exc
        if not isinstance(container, list):
            raise MESSyncError(f"MES {entity} 列表路径 {items_path or '（根节点）'} 不是数组")
        field_map = config.get("field_map") or {}
        if not isinstance(field_map, dict):
            raise MESMasterError(f"MES {entity} field_map 必须是 JSON 对象")
        items = []
        for entry in container:
            if not isinstance(entry, dict):
                continue
            mapped: dict[str, Any] = {"raw": entry}
            for field in ENTITY_FIELDS[entity]:
                source_path = str(field_map.get(field) or field).strip() or field
                try:
                    value = resolve_path(entry, source_path)
                except KeyError:
                    value = ""
                mapped[field] = "" if value is None else value
            items.append(mapped)
        return items

    # ------------------------------------------------------------------ 查询
    def list_items(self, entity: str, include_disabled: bool = True) -> dict[str, Any]:
        self._require_entity(entity)
        where = "" if include_disabled else "WHERE enabled = 1"
        rows = self.db.all(f"SELECT * FROM mes_{entity} {where} ORDER BY code")
        last_sync = self.db.one(f"SELECT MAX(synced_at) AS last FROM mes_{entity} WHERE source = 'mes'")["last"]
        return {
            "mode": str(self.master_config().get("mode", "simulator")),
            "last_sync": last_sync,
            "items": [self._serialize(entity, row) for row in rows],
        }

    def get_item(self, entity: str, code: str) -> dict[str, Any]:
        self._require_entity(entity)
        row = self.db.one(f"SELECT * FROM mes_{entity} WHERE code = ?", (code,))
        if row is None:
            raise MESMasterMissingError(f"编号不存在：{code}")
        return self._serialize(entity, row)

    @staticmethod
    def _serialize(entity: str, row: sqlite3.Row) -> dict[str, Any]:
        item: dict[str, Any] = {field: row[field] for field in ENTITY_FIELDS[entity]}
        item.update(
            source=row["source"],
            enabled=bool(row["enabled"]),
            synced_at=row["synced_at"],
            updated_at=row["updated_at"],
        )
        return item

    # ------------------------------------------------------------------ 本地维护
    def create_item(self, entity: str, data: dict[str, Any]) -> dict[str, Any]:
        self._require_entity(entity)
        fields = ENTITY_FIELDS[entity]
        code = str(data.get("code", "")).strip()
        if not code:
            raise MESMasterError("编号不能为空")
        values = [str(data.get(field, "")).strip()[:200] for field in fields[1:]]
        if not values[0]:
            raise MESMasterError("名称不能为空")
        now = utc_now()
        placeholders = ", ".join("?" * len(fields))
        try:
            with self.db.transaction() as connection:
                connection.execute(
                    f"INSERT INTO mes_{entity}(code, {', '.join(fields[1:])}, source, enabled, raw_json, "
                    f"created_at, updated_at) VALUES ({placeholders}, 'local', 1, '{{}}', ?, ?)",
                    (code, *values, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise MESMasterConflictError(f"编号已存在：{code}") from exc
        return self.get_item(entity, code)

    def update_item(self, entity: str, code: str, data: dict[str, Any]) -> dict[str, Any]:
        self._require_entity(entity)
        fields = ENTITY_FIELDS[entity]
        current = self.db.one(f"SELECT * FROM mes_{entity} WHERE code = ?", (code,))
        if current is None:
            raise MESMasterMissingError(f"编号不存在：{code}")
        now = utc_now()
        if current["source"] == "mes":
            enabled = data.get("enabled")
            if not isinstance(enabled, bool):
                raise MESMasterError("MES 同步的数据仅支持启用/停用，名称与属性请在 MES 中维护后再同步")
            self.db.execute(
                f"UPDATE mes_{entity} SET enabled = ?, updated_at = ? WHERE code = ?",
                (1 if enabled else 0, now, code),
            )
            return self.get_item(entity, code)
        values = [str(data.get(field, current[field])).strip()[:200] for field in fields[1:]]
        if not values[0]:
            raise MESMasterError("名称不能为空")
        enabled = data.get("enabled")
        self.db.execute(
            f"UPDATE mes_{entity} SET {', '.join(f'{field} = ?' for field in fields[1:])}, "
            "enabled = ?, updated_at = ? WHERE code = ?",
            (
                *values,
                current["enabled"] if not isinstance(enabled, bool) else (1 if enabled else 0),
                now,
                code,
            ),
        )
        return self.get_item(entity, code)

    def delete_item(self, entity: str, code: str) -> None:
        self._require_entity(entity)
        current = self.db.one(f"SELECT source FROM mes_{entity} WHERE code = ?", (code,))
        if current is None:
            raise MESMasterMissingError(f"编号不存在：{code}")
        if current["source"] == "mes":
            raise MESMasterConflictError("该数据来自 MES 同步，不能本地删除；可在 MES 端删除后同步，或在本地停用")
        self.db.execute(f"DELETE FROM mes_{entity} WHERE code = ? AND source = 'local'", (code,))
