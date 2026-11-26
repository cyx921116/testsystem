from __future__ import annotations

import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from .database import DEFAULT_FLASHER_CONFIG, Database
from .flasher import FlashError, build_flash_record, create_flasher
from .protocols import BaseProtocol, ProtocolError, create_protocol
from .utils import json_dumps, new_id, render_template, utc_now


class StepFailure(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        actual: Any = None,
        expected: Any = None,
        assertions: list[dict[str, Any]] | None = None,
    ):
        super().__init__(message)
        self.actual = actual
        self.expected = expected
        self.assertions = assertions or []


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _elapsed_ms(started_at: str) -> int:
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    return max(0, round((datetime.now(UTC) - started).total_seconds() * 1000))


def _assert(actual: Any, operator: str, expected: Any) -> bool:
    if operator == "equals":
        return actual == expected
    if operator == "not_equals":
        return actual != expected
    if operator == "contains":
        return actual is not None and expected in actual
    if operator == "not_contains":
        return actual is None or expected not in actual
    if operator == "regex":
        return actual is not None and re.search(str(expected), str(actual)) is not None
    if operator in {"gt", "gte", "lt", "lte", "between"}:
        number = float(actual)
        if operator == "between":
            return float(expected[0]) <= number <= float(expected[1])
        target = float(expected)
        return {
            "gt": number > target,
            "gte": number >= target,
            "lt": number < target,
            "lte": number <= target,
        }[operator]
    if operator == "truthy":
        return bool(actual)
    if operator == "exists":
        return actual is not None and actual != ""
    raise ValueError(f"未知断言操作符：{operator}")


class TestExecutor:
    def __init__(self, database: Database):
        self.db = database
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="production-test")

    def submit(self, run_id: str) -> None:
        self.pool.submit(self._run_safely, run_id)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=True)

    def _protocol(self, name: str, cache: dict[str, BaseProtocol], context: dict[str, Any]) -> BaseProtocol:
        if name not in cache:
            row = self.db.one("SELECT config_json FROM protocol_configs WHERE name = ?", (name,))
            if row is None:
                raise ProtocolError(f"通讯协议 {name} 尚未配置")
            cache[name] = create_protocol(name, json.loads(row["config_json"]))
        protocol = cache[name]
        if hasattr(protocol, "set_context"):
            protocol.set_context(context)
        return protocol

    def _flasher(self, cache: dict[str, Any]) -> Any:
        if "active" not in cache:
            config = self.db.setting("flasher", DEFAULT_FLASHER_CONFIG)
            cache["active"] = create_flasher(config.get("backend", "simulator"), config.get("config") or {})
        return cache["active"]

    def _record_flash(self, request: dict[str, Any], result: dict[str, Any], context: dict[str, Any]) -> None:
        record = build_flash_record(request, result, context)
        firmware = record.get("firmware") or {}
        self.db.execute(
            "INSERT INTO flash_records(run_id, item_path, sn, station, operator, firmware_name, firmware_version, "
            "firmware_sha256, backend, ok, message, duration_ms, details_json, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["run_id"],
                record["item"],
                record["sn"],
                record["station"],
                record["operator"],
                firmware.get("name", ""),
                firmware.get("version", ""),
                firmware.get("sha256", ""),
                record["backend"],
                int(record["ok"]),
                record["message"],
                int(record["duration_ms"]),
                json_dumps(record["details"]),
                record["finished_at"],
            ),
        )

    def _execute_flash(
        self,
        action: dict[str, Any],
        step: dict[str, Any],
        context: dict[str, Any],
        flashers: dict[str, Any],
        on_progress: Any,
        item_path: str,
    ) -> dict[str, Any]:
        flasher = self._flasher(flashers)
        request = {
            "firmware": render_template(action["firmware"], context),
            "sn": str(context.get("sn", "")),
            "target": render_template(action.get("target", ""), context),
            "options": render_template(action.get("options", {}), context),
            "variables": render_template(action.get("variables", {}), context),
        }
        record_context = {
            "run_id": str(context.get("run_id", "")),
            "item": item_path or str(step.get("id", "")),
            "station": str(context.get("station", "")),
            "operator": str(context.get("operator", "")),
        }
        try:
            result = flasher.flash(request, on_progress=on_progress)
        except FlashError as exc:
            self._record_flash(request, {
                "ok": False,
                "backend": flasher.name,
                "firmware": {},
                "message": str(exc),
                "duration_ms": 0,
                "details": {},
                "finished_at": utc_now(),
            }, record_context)
            raise StepFailure(f"固件烧录失败：{exc}") from exc
        self._record_flash(request, result, record_context)
        return result

    def _execute_actions(
        self,
        step: dict[str, Any],
        context: dict[str, Any],
        protocols: dict[str, BaseProtocol],
        flashers: dict[str, Any] | None = None,
        on_progress: Any = None,
        item_path: str = "",
    ) -> tuple[Any, Any, list[dict[str, Any]]]:
        started = time.monotonic()
        last_value: Any = None
        last_expected: Any = None
        assertions: list[dict[str, Any]] = []
        for action in step["actions"]:
            elapsed_ms = (time.monotonic() - started) * 1000
            if elapsed_ms > int(step["timeout_ms"]):
                raise StepFailure(
                    f"测试项超过 {step['timeout_ms']} ms 超时限制",
                    actual=last_value,
                    expected=last_expected,
                    assertions=assertions,
                )
            action_type = action["type"]
            if action_type == "sleep":
                time.sleep(int(action["duration_ms"]) / 1000)
                continue
            if action_type == "set":
                context[action["variable"]] = render_template(action.get("value"), context)
                continue
            if action_type == "flash":
                result = self._execute_flash(
                    action, step, context, flashers if flashers is not None else {}, on_progress, item_path
                )
                last_value = result["message"]
                context["last_response"] = result
                if action.get("save_as"):
                    context[action["save_as"]] = result
                continue

            protocol = self._protocol(step["protocol"], protocols, context)
            if action_type == "request":
                data = render_template(action["data"], context)
                remaining = max(1, int(step["timeout_ms"] - elapsed_ms))
                last_value = protocol.request(data, action.get("encoding"), remaining)
                context["last_response"] = last_value
                if action.get("save_as"):
                    context[action["save_as"]] = last_value
            elif action_type == "send":
                protocol.send(render_template(action["data"], context), action.get("encoding"))
            elif action_type == "receive":
                last_value = protocol.receive(int(action.get("timeout_ms", 1000)), int(action.get("size", 4096)))
                context["last_response"] = last_value
                context[action["save_as"]] = last_value
            elif action_type == "execute":
                params = render_template(action.get("params", {}), context)
                last_value = protocol.execute(str(action["operation"]), params)
                context["last_response"] = last_value
                if action.get("save_as"):
                    context[action["save_as"]] = last_value
            elif action_type == "assert":
                actual = render_template(action["actual"], context)
                expected = render_template(action.get("expected"), context)
                last_value = actual
                last_expected = expected
                try:
                    passed = _assert(actual, action["operator"], expected)
                except (TypeError, ValueError, IndexError) as exc:
                    raise StepFailure(
                        f"断言数据无法比较：{exc}",
                        actual=actual,
                        expected=expected,
                        assertions=assertions,
                    ) from exc
                detail = {
                    "actual": actual,
                    "operator": action["operator"],
                    "expected": expected,
                    "passed": passed,
                }
                assertions.append(detail)
                if not passed:
                    message = action.get("message") or f"断言失败：实际 {_text(actual)!r}，期望 {action['operator']} {_text(expected)!r}"
                    raise StepFailure(message, actual=actual, expected=expected, assertions=assertions)
        return last_value, last_expected, assertions

    def _flash_progress(self, run_id: str, item_id: str) -> Any:
        def report(percent: int, message: str) -> None:
            self.db.execute(
                "UPDATE test_items SET message = ? WHERE run_id = ? AND item_id = ?",
                (f"烧录 {percent}%：{message}", run_id, item_id),
            )

        return report

    def _run_safely(self, run_id: str) -> None:
        protocols: dict[str, BaseProtocol] = {}
        flashers: dict[str, Any] = {}
        try:
            self._execute_run(run_id, protocols, flashers)
        except Exception as exc:
            now = utc_now()
            self.db.execute(
                "UPDATE test_items SET status = 'FAIL', message = ?, finished_at = ? "
                "WHERE run_id = ? AND status IN ('PENDING', 'RUNNING')",
                (f"测试执行异常：{exc}", now, run_id),
            )
            self.db.execute(
                "UPDATE test_runs SET error = ? WHERE id = ?",
                (str(exc), run_id),
            )
        finally:
            for protocol in protocols.values():
                try:
                    protocol.close()
                except Exception:
                    pass
            for flasher in flashers.values():
                try:
                    flasher.close()
                except Exception:
                    pass
        try:
            report_id = self._finalize(run_id)
            if self.db.setting("auto_upload", False):
                from .mes import MESUploader

                try:
                    MESUploader(self.db).upload(report_id)
                except Exception:
                    pass
        except Exception:
            self.db.execute(
                "UPDATE test_runs SET status = 'FAIL', error = ?, finished_at = ? WHERE id = ?",
                ("报告生成失败，请联系管理员", utc_now(), run_id),
            )

    def _execute_run(self, run_id: str, protocols: dict[str, BaseProtocol], flashers: dict[str, Any]) -> None:
        run = self.db.one("SELECT * FROM test_runs WHERE id = ?", (run_id,))
        if run is None or run["status"] != "RUNNING":
            return
        script = json.loads(run["script_snapshot_json"])
        if script.get("schema_version") == 2:
            from .control_flow import ControlFlowRunner

            ControlFlowRunner(self, run, script, protocols, flashers).execute()
            return
        context: dict[str, Any] = {
            **script.get("variables", {}),
            "sn": run["sn"],
            "station": run["station"],
            "operator": run["operator"],
            "run_id": run_id,
            "product_model": run["product_model"],
        }
        stop_remaining = False
        for index, step in enumerate(script["steps"]):
            if stop_remaining:
                self.db.execute(
                    "UPDATE test_items SET status = 'FAIL', message = ?, finished_at = ? WHERE run_id = ? AND item_id = ?",
                    ("前序测试失败，本项未执行", utc_now(), run_id, step["id"]),
                )
                continue

            item_started = utc_now()
            monotonic_start = time.monotonic()
            self.db.execute(
                "UPDATE test_runs SET current_item = ? WHERE id = ?",
                (step["id"], run_id),
            )
            self.db.execute(
                "UPDATE test_items SET status = 'RUNNING', started_at = ?, message = ? WHERE run_id = ? AND item_id = ?",
                (item_started, "正在测试", run_id, step["id"]),
            )
            final_status = "FAIL"
            final_message = "测试失败"
            actual: Any = None
            expected: Any = None
            assertions: list[dict[str, Any]] = []
            max_attempts = int(step["retries"]) + 1
            attempts = 0
            for attempt in range(1, max_attempts + 1):
                attempts = attempt
                try:
                    actual, expected, assertions = self._execute_actions(
                        step, context, protocols, flashers, self._flash_progress(run_id, step["id"]), step["id"]
                    )
                    final_status = "PASS"
                    final_message = "测试通过"
                    break
                except (StepFailure, ProtocolError, OSError, ValueError) as exc:
                    if isinstance(exc, StepFailure):
                        actual = exc.actual
                        expected = exc.expected
                        assertions = exc.assertions
                    final_message = str(exc)
                    if attempt < attempts:
                        self.db.execute(
                            "UPDATE test_items SET message = ? WHERE run_id = ? AND item_id = ?",
                            (f"第 {attempt} 次失败，正在重试", run_id, step["id"]),
                        )
            duration = round((time.monotonic() - monotonic_start) * 1000)
            self.db.execute(
                "UPDATE test_items SET status = ?, expected = ?, actual = ?, message = ?, attempts = ?, "
                "details_json = ?, finished_at = ?, duration_ms = ? WHERE run_id = ? AND item_id = ?",
                (
                    final_status,
                    _text(expected),
                    _text(actual),
                    final_message,
                    attempts,
                    json_dumps(assertions),
                    utc_now(),
                    duration,
                    run_id,
                    step["id"],
                ),
            )
            if final_status == "FAIL" and step["on_fail"] == "stop":
                stop_remaining = True

    def _finalize(self, run_id: str) -> str:
        with self.db.transaction() as connection:
            existing = connection.execute("SELECT id FROM reports WHERE run_id = ?", (run_id,)).fetchone()
            if existing:
                return existing["id"]
            run = connection.execute("SELECT * FROM test_runs WHERE id = ?", (run_id,)).fetchone()
            rows = connection.execute(
                "SELECT * FROM test_items WHERE run_id = ? ORDER BY sequence", (run_id,)
            ).fetchall()
            finished_at = utc_now()
            items = []
            for row in rows:
                items.append({
                    "item_id": row["item_id"],
                    "node_id": row["node_id"] or row["item_id"],
                    "path": row["path"] or row["item_id"],
                    "iteration": row["iteration"],
                    "name": row["item_name"],
                    "sequence": row["sequence"],
                    "status": "FAIL" if row["status"] in {"PENDING", "RUNNING"} else row["status"],
                    "expected": row["expected"],
                    "actual": row["actual"],
                    "message": row["message"],
                    "attempts": row["attempts"],
                    "assertions": json.loads(row["details_json"]),
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "duration_ms": row["duration_ms"],
                })
            passed = sum(item["status"] == "PASS" for item in items)
            failed = len(items) - passed
            status = "PASS" if failed == 0 and items and not run["error"] else "FAIL"
            duration_ms = _elapsed_ms(run["started_at"])
            report_id = new_id("report")
            report_no = f"RPT-{datetime.now(UTC):%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
            summary = {"total": len(items), "passed": passed, "failed": failed}
            controls = connection.execute(
                "SELECT * FROM control_events WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
            flash_records = connection.execute(
                "SELECT * FROM flash_records WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
            if any(control["status"] in {"FAIL", "RUNNING", "INTERRUPTED"} for control in controls):
                status = "FAIL"
            report = {
                "schema_version": "1.0",
                "id": report_id,
                "report_no": report_no,
                "sn": run["sn"],
                "run_id": run_id,
                "product_model": run["product_model"],
                "station": run["station"],
                "operator": run["operator"],
                "script": {
                    "id": run["script_id"],
                    "name": json.loads(run["script_snapshot_json"])["name"],
                    "version": run["script_version"],
                },
                "status": status,
                "started_at": run["started_at"],
                "finished_at": finished_at,
                "duration_ms": duration_ms,
                "summary": summary,
                "items": items,
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
                        "firmware": {
                            "name": record["firmware_name"],
                            "version": record["firmware_version"],
                            "sha256": record["firmware_sha256"],
                        },
                        "backend": record["backend"],
                        "ok": bool(record["ok"]),
                        "message": record["message"],
                        "duration_ms": record["duration_ms"],
                        "details": json.loads(record["details_json"]),
                        "finished_at": record["finished_at"],
                    }
                    for record in flash_records
                ],
            }
            connection.execute(
                "UPDATE test_runs SET status = ?, current_item = NULL, finished_at = ? WHERE id = ?",
                (status, finished_at, run_id),
            )
            connection.execute(
                "UPDATE products SET last_tested_at = ? WHERE sn = ?",
                (finished_at, run["sn"]),
            )
            connection.execute(
                "INSERT INTO reports(id, report_no, sn, run_id, product_model, station, operator, script_name, "
                "script_version, status, summary_json, report_json, started_at, finished_at, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report_id,
                    report_no,
                    run["sn"],
                    run_id,
                    run["product_model"],
                    run["station"],
                    run["operator"],
                    report["script"]["name"],
                    run["script_version"],
                    status,
                    json_dumps(summary),
                    json_dumps(report),
                    run["started_at"],
                    finished_at,
                    duration_ms,
                ),
            )
            return report_id
