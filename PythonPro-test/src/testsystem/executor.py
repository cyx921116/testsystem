"""测试执行器：在后台线程中运行测试流程，落库逐项结果并生成唯一报告。

结构：
- ``TestExecutor`` 用单工作线程池串行执行运行（单工位一次只测一台产品，
  也避免并发访问同一套硬件；并行需求由控制流 parallel 节点在运行内部解决）；
- v1 步骤式流程由 ``_execute_run`` 顺序执行，v2 控制流流程转交 ControlFlowRunner；
- 动作执行（``_execute_actions``）与断言（``_assert``）为两种流程共用；
- 运行结束统一走 ``_finalize``：汇总测试项/控制流/烧录记录生成唯一正式报告，
  并按工位策略决定是否自动上传 MES。
"""
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
from .utils import json_dumps, new_id, render_template, resolve_value, utc_now


class StepFailure(RuntimeError):
    """测试项失败：携带实际值/期望值/断言明细，供报告展示失败原因。"""

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
    """把任意值转为可入库的展示文本（对象转 JSON，None 转空串）。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _elapsed_ms(started_at: str) -> int:
    """从 ISO 时间戳到当前的毫秒数（报告总耗时）。"""
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    return max(0, round((datetime.now(UTC) - started).total_seconds() * 1000))


def _assert(actual: Any, operator: str, expected: Any) -> bool:
    """断言判定：相等/包含/正则/数值比较/区间/为真/非空。

    数值类比较会把两侧转为 float，转换失败抛 ValueError，由调用方包装为
    “断言数据无法比较”的测试项失败。
    """
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
    """后台执行器：接收运行 ID，串行执行并负责收尾（报告 + 自动上传）。"""

    def __init__(self, database: Database):
        self.db = database
        # 单工作线程：运行串行执行，避免并发争用串口等硬件；多运行自动排队
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="production-test")

    def submit(self, run_id: str) -> None:
        """把运行提交到后台线程（立即返回，前端轮询运行状态）。"""
        self.pool.submit(self._run_safely, run_id)

    def shutdown(self) -> None:
        """服务停止时等待当前运行结束并取消排队任务。"""
        self.pool.shutdown(wait=True, cancel_futures=True)

    def _protocol(self, name: str, cache: dict[str, BaseProtocol], context: dict[str, Any]) -> BaseProtocol:
        """按协议名取本次运行的协议连接（懒创建并缓存），并向仿真器注入运行上下文。"""
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
        """取本次运行的烧录器（按工位烧录配置懒创建并缓存）。"""
        if "active" not in cache:
            config = self.db.setting("flasher", DEFAULT_FLASHER_CONFIG)
            cache["active"] = create_flasher(config.get("backend", "simulator"), config.get("config") or {})
        return cache["active"]

    def _record_flash(self, request: dict[str, Any], result: dict[str, Any], context: dict[str, Any]) -> None:
        """烧录留痕：无论成败都写入 flash_records（固件、校验和、结果），供报告追溯。"""
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
        """执行固件烧录动作：渲染模板参数 → 调烧录后端 → 留痕；失败转为 StepFailure。"""
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
        """顺序执行一个测试项的全部动作，返回（最后值, 期望值, 断言明细）。

        每个动作前检查测试项级超时；通讯响应存入 last_response 与 save_as 变量
        供后续动作/断言用 ${} 引用；断言失败立即抛 StepFailure 中断本测试项。
        """
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
                # 请求超时取“测试项剩余预算”，避免单动作耗尽整个测试项时限
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
                actual = resolve_value(action["actual"], context)
                expected = resolve_value(action.get("expected"), context)
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
        """生成烧录进度回调：把百分比与阶段消息写到测试项 message，前端轮询可见。"""
        def report(percent: int, message: str) -> None:
            self.db.execute(
                "UPDATE test_items SET message = ? WHERE run_id = ? AND item_id = ?",
                (f"烧录 {percent}%：{message}", run_id, item_id),
            )

        return report

    def _run_safely(self, run_id: str) -> None:
        """运行外壳：兜底捕获一切异常（运行标记失败），最后统一生成报告并按需上传 MES。"""
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
            # 关闭本次运行打开的所有硬件连接（正常/异常路径都要释放）
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
                    # 自动上传失败不阻塞测试流程：报告已保留 FAILED 状态，可在报告中心重试
                    pass
        except Exception:
            self.db.execute(
                "UPDATE test_runs SET status = 'FAIL', error = ?, finished_at = ? WHERE id = ?",
                ("报告生成失败，请联系管理员", utc_now(), run_id),
            )

    def _execute_run(self, run_id: str, protocols: dict[str, BaseProtocol], flashers: dict[str, Any]) -> None:
        """执行一次运行：v2 控制流交 ControlFlowRunner；v1 逐步骤执行并处理重试/失败策略。"""
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
                # on_fail=stop 的失败项之后的所有步骤标记为未执行
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
        """收尾：汇总测试项/控制流/烧录记录生成唯一正式报告，返回报告 ID。

        整体结果判定：全部测试项通过且无执行错误、无失败/中断的控制节点才是 PASS；
        每次运行（含复测）各生成一份报告永久留档，失败报告不再删除。
        """
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
                    # 收尾时仍处于 PENDING/RUNNING 的项按失败计入（异常中断场景）
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
            # 失败报告不再删除：每次测试（含每次复测）都会生成一份独立报告永久留档，
            # 并各自等待上传 MES，保证失败追溯数据完整。
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
