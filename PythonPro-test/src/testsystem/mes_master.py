"""MES 主数据：产品/工站的同步、查询与本地维护。

MES 是产品与工站编号的权威来源，本地缓存于 mes_products / mes_stations 表：
- 同步（sync）：simulator 模式用内置示例数据（SIMULATOR_PRODUCTS/STATIONS），
  http 模式按 MES 配置（地址、列表路径、字段映射）拉取；以 code 为准 upsert，
  MES 响应中消失的行自动停用，本地添加的行不受同步影响；
- 本地维护：本地行可增删改，MES 行只能启用/停用（权威数据不可本地篡改）；
- 主数据为空时脚本绑定保持自由文本模式，便于 demo 与离线使用。
"""
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
    {"code": "MAIN-100", "name": "主控板", "spec": "V1.0"},
    {"code": "MAIN-200", "name": "主控板 Pro", "spec": "V2.0"},
    {"code": "DRV-110", "name": "电机驱动板", "spec": "48V/10A"},
    {"code": "DRV-210", "name": "电机驱动板", "spec": "72V/20A"},
    {"code": "PWR-101", "name": "电源管理板", "spec": "12V"},
    {"code": "PWR-201", "name": "电源管理板", "spec": "24V"},
    {"code": "SEN-310", "name": "温度传感器模组", "spec": "-40~125℃"},
    {"code": "SEN-320", "name": "压力传感器模组", "spec": "0~10MPa"},
    {"code": "SEN-330", "name": "流量计模组", "spec": "脉冲输出"},
    {"code": "COMM-410", "name": "4G 通信模组", "spec": "CAT1"},
    {"code": "COMM-420", "name": "WiFi 通信模组", "spec": "2.4G"},
    {"code": "COMM-430", "name": "CAN 网关", "spec": "2 路 CAN"},
    {"code": "DISP-510", "name": "显示模组", "spec": "4.3 寸"},
    {"code": "DISP-520", "name": "显示模组", "spec": "7 寸"},
    {"code": "BAT-610", "name": "电池包", "spec": "48V 20Ah"},
    {"code": "BAT-620", "name": "充电管理板", "spec": "3A"},
    {"code": "RF-710", "name": "射频板", "spec": "Sub-1G"},
    {"code": "RF-720", "name": "蓝牙模组", "spec": "BLE5.0"},
    {"code": "AUD-810", "name": "音频处理板", "spec": "双麦克风"},
    {"code": "CAM-910", "name": "摄像头模组", "spec": "200W"},
    {"code": "KEY-110", "name": "控制面板", "spec": "16 键"},
    {"code": "ADP-120", "name": "测试转接板", "spec": "通用"},
]

SIMULATOR_STATIONS = [
    {"code": "STATION-01", "name": "1 号功能测试工站", "line": "L01", "station_type": "FUNCTIONAL"},
    {"code": "STATION-02", "name": "2 号功能测试工站", "line": "L01", "station_type": "FUNCTIONAL"},
    {"code": "ICT-01", "name": "ICT 测试工站", "line": "L02", "station_type": "ICT"},
    {"code": "ICT-02", "name": "ICT 测试工站 2", "line": "L02", "station_type": "ICT"},
    {"code": "FCT-01", "name": "功能测试工站", "line": "L01", "station_type": "FCT"},
    {"code": "FCT-02", "name": "功能测试工站 2", "line": "L01", "station_type": "FCT"},
    {"code": "FLASH-01", "name": "烧录工站", "line": "L03", "station_type": "FLASH"},
    {"code": "PROG-01", "name": "程序写入工站", "line": "L03", "station_type": "FLASH"},
    {"code": "BURN-01", "name": "老化测试工站", "line": "L04", "station_type": "BURN_IN"},
    {"code": "BURN-02", "name": "老化测试工站 2", "line": "L04", "station_type": "BURN_IN"},
    {"code": "CAL-01", "name": "校准工站", "line": "L05", "station_type": "CALIBRATION"},
    {"code": "HI-01", "name": "高温测试工站", "line": "L04", "station_type": "ENVIRONMENT"},
    {"code": "VIB-01", "name": "振动测试工站", "line": "L04", "station_type": "ENVIRONMENT"},
    {"code": "RF-01", "name": "射频测试工站", "line": "L05", "station_type": "RF"},
    {"code": "AUD-01", "name": "音频测试工站", "line": "L05", "station_type": "AUDIO"},
    {"code": "AOI-01", "name": "AOI 外观检测工站", "line": "L02", "station_type": "INSPECTION"},
    {"code": "EOL-01", "name": "终检工站", "line": "L06", "station_type": "INSPECTION"},
    {"code": "SAFE-01", "name": "安规测试工站", "line": "L06", "station_type": "SAFETY"},
    {"code": "REW-01", "name": "返修测试工站", "line": "L06", "station_type": "REWORK"},
    {"code": "PACK-01", "name": "包装工站", "line": "L06", "station_type": "PACKAGING"},
]

# 每个实体允许的目标字段（首列为主键），同步映射和本地维护都以此为白名单。
ENTITY_FIELDS = {
    "products": ["code", "name", "spec"],
    "stations": ["code", "name", "line", "station_type"],
}


class MESMasterData:
    """主数据服务：封装两种来源（模拟器/HTTP）的同步与本地维护规则。"""

    def __init__(self, database: Database):
        self.db = database

    def master_config(self) -> dict[str, Any]:
        """从 MES 配置中取出 master_data 段（缺失时返回空字典，按模拟器模式处理）。"""
        mes = self.db.setting("mes", {}) or {}
        master = mes.get("master_data")
        return master if isinstance(master, dict) else {}

    @staticmethod
    def _require_entity(entity: str) -> None:
        """实体白名单校验：只允许 products / stations，防注入拼表名。"""
        if entity not in ENTITY_FIELDS:
            raise MESMasterError(f"未知的主数据类型：{entity}")

    # ------------------------------------------------------------------ 同步
    def sync(self, target: str) -> dict[str, Any]:
        """同步入口：target 为 products / stations / both，返回各实体的新增/更新/停用统计。"""
        if target not in {"products", "stations", "both"}:
            raise MESMasterError("同步目标必须是 products、stations 或 both")
        entities = ("products", "stations") if target == "both" else (target,)
        return {entity: self._sync_entity(entity) for entity in entities}

    def _sync_entity(self, entity: str) -> dict[str, Any]:
        """同步单个实体：按 code upsert MES 行，响应中消失的 MES 行停用（本地行不动）。"""
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
        """按主数据模式取数：simulator 返回内置示例，http 走真实接口。"""
        mode = str(self.master_config().get("mode", "simulator")).strip().lower()
        if mode == "simulator":
            source = SIMULATOR_PRODUCTS if entity == "products" else SIMULATOR_STATIONS
            return [dict(item, raw=dict(item)) for item in source]
        if mode == "http":
            return self._fetch_http(entity, config)
        raise MESMasterError(f"未知的主数据模式：{mode}，仅支持 simulator 或 http")

    def _fetch_http(self, entity: str, config: dict[str, Any]) -> list[dict[str, Any]]:
        """HTTP 模式取数：GET/POST 查询 MES，按 items_path 定位列表、field_map 映射字段。"""
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
        """列出主数据（可按需隐藏停用行），附带数据模式与最近同步时间。"""
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
        """按编号取单条主数据；不存在抛 MESMasterMissingError（映射为 404）。"""
        self._require_entity(entity)
        row = self.db.one(f"SELECT * FROM mes_{entity} WHERE code = ?", (code,))
        if row is None:
            raise MESMasterMissingError(f"编号不存在：{code}")
        return self._serialize(entity, row)

    @staticmethod
    def _serialize(entity: str, row: sqlite3.Row) -> dict[str, Any]:
        """输出行数据：白名单字段 + 来源/启用/时间戳（raw_json 仅内部留档，不外发）。"""
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
        """添加本地行（source='local'）：编号/名称必填，编号重复抛冲突错误（409）。"""
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
        """更新主数据：MES 行仅允许启用/停用（权威数据在 MES 侧维护），本地行可改名称与属性。"""
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
        """删除本地行；MES 同步行禁止本地删除（可在 MES 端删除后同步，或本地停用）。"""
        self._require_entity(entity)
        current = self.db.one(f"SELECT source FROM mes_{entity} WHERE code = ?", (code,))
        if current is None:
            raise MESMasterMissingError(f"编号不存在：{code}")
        if current["source"] == "mes":
            raise MESMasterConflictError("该数据来自 MES 同步，不能本地删除；可在 MES 端删除后同步，或在本地停用")
        self.db.execute(f"DELETE FROM mes_{entity} WHERE code = ? AND source = 'local'", (code,))
