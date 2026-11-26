from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from typing import Any, TYPE_CHECKING

from .executor import StepFailure, _assert, _text
from .protocols import BaseProtocol, ProtocolError
from .utils import json_dumps, render_template, resolve_value, utc_now

if TYPE_CHECKING:
    from .executor import TestExecutor


class ControlFlowRunner:
    def __init__(
        self,
        service: TestExecutor,
        run: Any,
        script: dict[str, Any],
        protocols: dict[str, BaseProtocol],
        flashers: dict[str, Any] | None = None,
    ):
        self.service = service
        self.db = service.db
        self.run = run
        self.script = script
        self.protocols = protocols
        self.flashers = flashers if flashers is not None else {}
        self.sequence = 0
        self.sequence_lock = threading.Lock()

    def execute(self) -> None:
        context: dict[str, Any] = {
            **self.script.get("variables", {}),
            "sn": self.run["sn"],
            "station": self.run["station"],
            "operator": self.run["operator"],
            "run_id": self.run["id"],
            "product_model": self.run["product_model"],
            "branches": {},
            "subflows": {},
        }
        self._sequence(self.script["steps"], context, self.protocols, self.flashers, "root", 0)

    def _next_sequence(self) -> int:
        with self.sequence_lock:
            self.sequence += 1
            return self.sequence

    def _sequence(
        self,
        steps: list[dict[str, Any]],
        context: dict[str, Any],
        protocols: dict[str, BaseProtocol],
        flashers: dict[str, Any],
        path: str,
        call_depth: int,
    ) -> bool:
        success = True
        for node in steps:
            node_path = f"{path}/{node['id']}"
            node_success = self._execute_node(node, node_path, context, protocols, flashers, call_depth)
            if not node_success:
                success = False
                if node["on_fail"] == "stop":
                    break
        return success

    def _execute_node(
        self,
        node: dict[str, Any],
        path: str,
        context: dict[str, Any],
        protocols: dict[str, BaseProtocol],
        flashers: dict[str, Any],
        call_depth: int,
    ) -> bool:
        node_type = node["type"]
        if node_type == "test":
            return self._execute_test(node, path, context, protocols, flashers)
        if node_type == "condition":
            return self._execute_condition(node, path, context, protocols, flashers, call_depth)
        if node_type == "parallel":
            return self._execute_parallel(node, path, context, call_depth)
        if node_type == "loop":
            return self._execute_loop(node, path, context, protocols, flashers, call_depth)
        if node_type == "subflow":
            return self._execute_subflow(node, path, context, protocols, flashers, call_depth)
        raise RuntimeError(f"未知控制流节点：{node_type}")

    def _execute_test(
        self,
        step: dict[str, Any],
        path: str,
        context: dict[str, Any],
        protocols: dict[str, BaseProtocol],
        flashers: dict[str, Any],
    ) -> bool:
        sequence = self._next_sequence()
        iteration = context.get("iteration")
        item_name = step["name"] if iteration is None else f"{step['name']}（第 {iteration} 次）"
        started_at = utc_now()
        monotonic_start = time.monotonic()
        self.db.execute(
            "UPDATE test_runs SET current_item = ? WHERE id = ?",
            (path, self.run["id"]),
        )
        self.db.execute(
            "INSERT INTO test_items(run_id, item_id, item_name, sequence, status, expected, actual, message, "
            "attempts, details_json, started_at, node_id, path, iteration) "
            "VALUES (?, ?, ?, ?, 'RUNNING', NULL, NULL, ?, 0, '[]', ?, ?, ?, ?) "
            "ON CONFLICT(run_id, item_id) DO UPDATE SET status = 'RUNNING', started_at = excluded.started_at, "
            "message = excluded.message",
            (
                self.run["id"],
                path,
                item_name,
                sequence,
                "正在测试",
                started_at,
                step["id"],
                path,
                iteration,
            ),
        )
        final_status = "FAIL"
        final_message = "测试失败"
        actual: Any = None
        expected: Any = None
        assertions: list[dict[str, Any]] = []
        attempts = 0
        for attempt in range(1, int(step["retries"]) + 2):
            attempts = attempt
            try:
                actual, expected, assertions = self.service._execute_actions(
                    step, context, protocols, flashers,
                    self.service._flash_progress(self.run["id"], path), path,
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
                if attempt < int(step["retries"]) + 1:
                    self.db.execute(
                        "UPDATE test_items SET message = ? WHERE run_id = ? AND item_id = ?",
                        (f"第 {attempt} 次失败，正在重试", self.run["id"], path),
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
                self.run["id"],
                path,
            ),
        )
        return final_status == "PASS"

    def _evaluate(
        self,
        condition: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[bool, Any, Any]:
        actual = resolve_value(condition.get("actual"), context)
        expected = resolve_value(condition.get("expected"), context)
        return _assert(actual, condition["operator"], expected), actual, expected

    def _start_control(self, node: dict[str, Any], path: str) -> str:
        started_at = utc_now()
        self.db.execute(
            "INSERT INTO control_events(run_id, node_id, node_name, node_type, path, status, details_json, started_at) "
            "VALUES (?, ?, ?, ?, ?, 'RUNNING', '{}', ?) "
            "ON CONFLICT(run_id, path) DO UPDATE SET status = 'RUNNING', started_at = excluded.started_at",
            (self.run["id"], node["id"], node["name"], node["type"], path, started_at),
        )
        return started_at

    def _finish_control(self, path: str, status: str, details: Any, started_at: str) -> None:
        self.db.execute(
            "UPDATE control_events SET status = ?, details_json = ?, finished_at = ? WHERE run_id = ? AND path = ?",
            (status, json_dumps(details), utc_now(), self.run["id"], path),
        )

    def _execute_condition(
        self,
        node: dict[str, Any],
        path: str,
        context: dict[str, Any],
        protocols: dict[str, BaseProtocol],
        flashers: dict[str, Any],
        call_depth: int,
    ) -> bool:
        started_at = self._start_control(node, path)
        try:
            matched, actual, expected = self._evaluate(node["condition"], context)
        except (TypeError, ValueError, IndexError) as exc:
            self._finish_control(path, "FAIL", {"error": str(exc)}, started_at)
            return False
        branch_name = "then" if matched else "else"
        branch_success = self._sequence(
            node[branch_name], context, protocols, flashers, f"{path}.{branch_name}", call_depth
        )
        self._finish_control(
            path,
            "TRUE" if matched else "FALSE",
            {
                "actual": actual,
                "expected": expected,
                "operator": node["condition"]["operator"],
                "branch": branch_name,
                "branch_success": branch_success,
            },
            started_at,
        )
        return branch_success

    def _execute_parallel(
        self,
        node: dict[str, Any],
        path: str,
        context: dict[str, Any],
        call_depth: int,
    ) -> bool:
        started_at = self._start_control(node, path)

        def branch_run(branch: dict[str, Any]) -> dict[str, Any]:
            branch_context = deepcopy(context)
            branch_protocols: dict[str, BaseProtocol] = {}
            branch_flashers: dict[str, Any] = {}
            try:
                success = self._sequence(
                    branch["steps"],
                    branch_context,
                    branch_protocols,
                    branch_flashers,
                    f"{path}.{branch['id']}",
                    call_depth,
                )
                return {"id": branch["id"], "success": success, "context": branch_context}
            finally:
                for protocol in branch_protocols.values():
                    try:
                        protocol.close()
                    except Exception:
                        pass
                for flasher in branch_flashers.values():
                    try:
                        flasher.close()
                    except Exception:
                        pass

        results: list[dict[str, Any]] = []
        workers = min(int(node["max_workers"]), len(node["branches"]))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"parallel-{node['id']}") as pool:
            futures = {pool.submit(branch_run, branch): branch for branch in node["branches"]}
            for future in as_completed(futures):
                branch = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append({"id": branch["id"], "success": False, "error": str(exc), "context": {}})
        results.sort(key=lambda item: item["id"])
        context.setdefault("branches", {})
        for result in results:
            context["branches"][result["id"]] = result.pop("context", {})
        success = all(result["success"] for result in results)
        self._finish_control(
            path,
            "PASS" if success else "FAIL",
            {
                "max_workers": workers,
                "branches": [
                    {key: value for key, value in result.items() if key != "context"}
                    for result in results
                ],
            },
            started_at,
        )
        return success

    def _execute_loop(
        self,
        node: dict[str, Any],
        path: str,
        context: dict[str, Any],
        protocols: dict[str, BaseProtocol],
        flashers: dict[str, Any],
        call_depth: int,
    ) -> bool:
        started_at = self._start_control(node, path)
        success = True
        iterations: list[dict[str, Any]] = []
        previous_iteration = context.get("iteration")
        previous_iteration0 = context.get("iteration0")
        limit = int(node["count"]) if node["mode"] == "count" else int(node["max_iterations"])
        for iteration in range(1, limit + 1):
            context["iteration"] = iteration
            context["iteration0"] = iteration - 1
            if node["mode"] == "while":
                try:
                    matched, actual, expected = self._evaluate(node["condition"], context)
                except (TypeError, ValueError, IndexError) as exc:
                    iterations.append({"iteration": iteration, "error": str(exc)})
                    success = False
                    break
                if not matched:
                    iterations.append({"iteration": iteration, "condition": False, "actual": actual, "expected": expected})
                    break
            iteration_success = self._sequence(
                node["steps"], context, protocols, flashers, f"{path}[{iteration}]", call_depth
            )
            iterations.append({"iteration": iteration, "success": iteration_success})
            if not iteration_success:
                success = False
                if node["break_on_fail"]:
                    break
        if previous_iteration is None:
            context.pop("iteration", None)
        else:
            context["iteration"] = previous_iteration
        if previous_iteration0 is None:
            context.pop("iteration0", None)
        else:
            context["iteration0"] = previous_iteration0
        self._finish_control(
            path,
            "PASS" if success else "FAIL",
            {"mode": node["mode"], "iterations": iterations},
            started_at,
        )
        return success

    def _execute_subflow(
        self,
        node: dict[str, Any],
        path: str,
        context: dict[str, Any],
        protocols: dict[str, BaseProtocol],
        flashers: dict[str, Any],
        call_depth: int,
    ) -> bool:
        started_at = self._start_control(node, path)
        if call_depth >= 6:
            self._finish_control(path, "FAIL", {"error": "子流程调用层级超过 6 层"}, started_at)
            return False
        definition = self.script.get("subflows", {}).get(node["subflow"])
        if definition is None:
            self._finish_control(path, "FAIL", {"error": f"子流程 {node['subflow']} 不存在"}, started_at)
            return False
        child_context = deepcopy(context)
        inputs = render_template(node.get("inputs", {}), context)
        child_context.update(inputs)
        success = self._sequence(
            definition["steps"], child_context, protocols, flashers, f"{path}<{node['subflow']}>", call_depth + 1
        )
        context.setdefault("subflows", {})[node["id"]] = child_context
        self._finish_control(
            path,
            "PASS" if success else "FAIL",
            {"subflow": node["subflow"], "inputs": inputs},
            started_at,
        )
        return success
