"""v2 控制流运行时：按节点类型执行条件/循环/并行/子流程，并记录执行轨迹。

执行模型：
- 变量上下文（context）贯穿整个运行，包含流程变量、SN/工站/操作员等内置变量，
  以及 branches（并行分支结果）、subflows（子流程结果）两个聚合入口；
- test 节点复用 v1 执行器的动作执行与断言逻辑（TestExecutor._execute_actions）；
- 控制节点的每次执行写入 control_events（路径唯一，重试覆盖），供报告回放；
- 并行分支使用独立线程池与独立的协议/烧录连接（串口等硬件句柄不能跨线程共享），
  分支上下文深拷贝隔离，结果按分支 ID 回写主上下文；
- 失败策略 on_fail=stop 时中断当前序列，continue 时继续下一节点。
"""
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
    """一次运行的控制流执行器：持有运行记录、协议连接池和节点序号发生器。"""

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
        # 并行分支会并发写测试项，序号发生器需要加锁保证唯一
        self.sequence_lock = threading.Lock()

    def execute(self) -> None:
        """执行入口：构造初始上下文（流程变量 + 内置变量），从主流程根节点开始。"""
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
        """线程安全的测试项序号（报告中的执行顺序）。"""
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
        """顺序执行一段节点序列；任一节点失败且其策略为 stop 时中断，返回整体成败。"""
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
        """按节点类型分派执行；未知类型属于编译漏网，直接抛错让运行失败。"""
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
        """执行测试项：登记 RUNNING 项 → 按重试次数执行动作序列 → 落库最终结果。

        路径（path）作为 item_id 保证循环/子流程中同一节点每次出现都有独立记录；
        循环内的迭代次数会拼进显示名（“第 N 次”）。
        """
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
        """求值条件断言：解析实际值/期望值后按操作符判定，返回（结果, 实际值, 期望值）。"""
        actual = resolve_value(condition.get("actual"), context)
        expected = resolve_value(condition.get("expected"), context)
        return _assert(actual, condition["operator"], expected), actual, expected

    def _start_control(self, node: dict[str, Any], path: str) -> str:
        """登记控制节点开始执行（同一路径重试时覆盖为最新一次）。"""
        started_at = utc_now()
        self.db.execute(
            "INSERT INTO control_events(run_id, node_id, node_name, node_type, path, status, details_json, started_at) "
            "VALUES (?, ?, ?, ?, ?, 'RUNNING', '{}', ?) "
            "ON CONFLICT(run_id, path) DO UPDATE SET status = 'RUNNING', started_at = excluded.started_at",
            (self.run["id"], node["id"], node["name"], node["type"], path, started_at),
        )
        return started_at

    def _finish_control(self, path: str, status: str, details: Any, started_at: str) -> None:
        """写入控制节点最终结果与明细（分支走向、迭代记录、输入参数等）。"""
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
        """条件节点：求值后执行 then/else 分支，控制事件记录走向与判定值。"""
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
        """并行节点：每个分支在独立线程中执行，使用独立的协议/烧录连接。

        分支上下文深拷贝隔离，结束后按分支 ID 回写主上下文的 branches 供后续断言；
        任一分支失败则整体失败。分支结束时关闭各自打开的硬件连接。
        """
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
        """循环节点：count 固定次数或 while 条件循环（max_iterations 兜底防死循环）。

        迭代序号写入上下文（iteration 从 1 起、iteration0 从 0 起）供动作模板使用，
        循环结束后恢复外层迭代变量，避免嵌套循环互相污染。
        """
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
        # 恢复外层迭代变量（嵌套循环场景）
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
        """子流程节点：深拷贝上下文并注入 inputs 实参后执行子流程体。

        运行期再做一次调用层级保护（编译期已限制 ≤6 层）；
        子流程的最终上下文按节点 ID 回写主上下文的 subflows 供后续断言引用。
        """
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
