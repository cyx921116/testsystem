from __future__ import annotations

from copy import deepcopy
from typing import Any

from .compiler import (
    ID_PATTERN,
    SUPPORTED_PROTOCOLS,
    ScriptValidationError,
    _number,
    _validate_action,
)


NODE_TYPES = {"test", "condition", "parallel", "loop", "subflow"}
ROOT_FLOW = "__main__"


def _register_id(node: dict[str, Any], path: str, errors: list[str], ids: set[str]) -> None:
    node_id = node.get("id")
    if not isinstance(node_id, str) or not ID_PATTERN.fullmatch(node_id):
        errors.append(f"{path}.id 需以字母开头，仅含字母、数字、下划线或连字符")
    elif node_id in ids:
        errors.append(f"{path}.id 重复：{node_id}")
    else:
        ids.add(node_id)
    if not str(node.get("name", "")).strip():
        errors.append(f"{path}.name 不能为空")
    if node.get("on_fail", "stop") not in {"stop", "continue"}:
        errors.append(f"{path}.on_fail 仅支持 stop 或 continue")
    node.setdefault("on_fail", "stop")


def _condition(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"{path} 必须是断言对象")
        return
    condition = {"type": "assert", **value}
    _validate_action(condition, path, errors)


def _sequence(
    steps: Any,
    path: str,
    errors: list[str],
    ids: set[str],
    depth: int,
    counter: list[int],
) -> None:
    if not isinstance(steps, list):
        errors.append(f"{path} 必须是节点数组")
        return
    if depth > 6:
        errors.append(f"{path} 嵌套层级超过 6 层")
        return
    for index, node in enumerate(steps):
        node_path = f"{path}[{index}]"
        counter[0] += 1
        if counter[0] > 500:
            errors.append("控制流节点总数不能超过 500")
            return
        if not isinstance(node, dict):
            errors.append(f"{node_path} 必须是对象")
            continue
        _register_id(node, node_path, errors, ids)
        node_type = node.get("type", "test")
        if node_type not in NODE_TYPES:
            errors.append(f"{node_path}.type 不支持：{node_type!r}")
            continue
        node["type"] = node_type
        if node_type == "test":
            if node.get("protocol") not in SUPPORTED_PROTOCOLS:
                errors.append(f"{node_path}.protocol 不受支持")
            _number(node.get("timeout_ms", 5000), f"{node_path}.timeout_ms", errors, 1, 300000)
            _number(node.get("retries", 0), f"{node_path}.retries", errors, 0, 10)
            actions = node.get("actions")
            if not isinstance(actions, list) or not actions:
                errors.append(f"{node_path}.actions 至少需要一个动作")
                actions = actions if isinstance(actions, list) else []
            if len(actions) > 50:
                errors.append(f"{node_path} 最多 50 个动作")
            for action_index, action in enumerate(actions):
                _validate_action(action, f"{node_path}.actions[{action_index}]", errors)
            node.setdefault("timeout_ms", 5000)
            node.setdefault("retries", 0)
        elif node_type == "condition":
            _condition(node.get("condition"), f"{node_path}.condition", errors)
            then_steps = node.get("then", [])
            else_steps = node.get("else", [])
            if not then_steps and not else_steps:
                errors.append(f"{node_path} 的 then 和 else 不能同时为空")
            _sequence(then_steps, f"{node_path}.then", errors, ids, depth + 1, counter)
            _sequence(else_steps, f"{node_path}.else", errors, ids, depth + 1, counter)
            node.setdefault("then", [])
            node.setdefault("else", [])
        elif node_type == "parallel":
            branches = node.get("branches")
            if not isinstance(branches, list) or not 2 <= len(branches) <= 8:
                errors.append(f"{node_path}.branches 必须包含 2~8 个分支")
                branches = branches if isinstance(branches, list) else []
            for branch_index, branch in enumerate(branches):
                branch_path = f"{node_path}.branches[{branch_index}]"
                if not isinstance(branch, dict):
                    errors.append(f"{branch_path} 必须是对象")
                    continue
                _register_id(branch, branch_path, errors, ids)
                _sequence(branch.get("steps"), f"{branch_path}.steps", errors, ids, depth + 1, counter)
            _number(node.get("max_workers", len(branches)), f"{node_path}.max_workers", errors, 1, 8)
            node.setdefault("max_workers", len(branches))
        elif node_type == "loop":
            mode = node.get("mode", "count")
            if mode not in {"count", "while"}:
                errors.append(f"{node_path}.mode 仅支持 count 或 while")
            if mode == "count":
                _number(node.get("count", 1), f"{node_path}.count", errors, 1, 100)
            else:
                _condition(node.get("condition"), f"{node_path}.condition", errors)
            _number(node.get("max_iterations", 20), f"{node_path}.max_iterations", errors, 1, 100)
            if not isinstance(node.get("break_on_fail", True), bool):
                errors.append(f"{node_path}.break_on_fail 必须是布尔值")
            _sequence(node.get("steps"), f"{node_path}.steps", errors, ids, depth + 1, counter)
            node.setdefault("count", 1)
            node.setdefault("max_iterations", 20)
            node.setdefault("break_on_fail", True)
        elif node_type == "subflow":
            if not isinstance(node.get("subflow"), str) or not ID_PATTERN.fullmatch(node["subflow"]):
                errors.append(f"{node_path}.subflow 必须引用合法子流程 ID")
            if not isinstance(node.get("inputs", {}), dict):
                errors.append(f"{node_path}.inputs 必须是对象")
            node.setdefault("inputs", {})


def _subflow_graph(steps: Any, subflows: dict[str, Any]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {ROOT_FLOW: set()}

    def visit(nodes: Any, owner: str) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_type = node.get("type", "test")
            if node_type == "subflow" and isinstance(node.get("subflow"), str):
                graph[owner].add(node["subflow"])
            elif node_type == "condition":
                visit(node.get("then", []), owner)
                visit(node.get("else", []), owner)
            elif node_type == "parallel":
                for branch in node.get("branches", []):
                    if isinstance(branch, dict):
                        visit(branch.get("steps", []), owner)
            elif node_type == "loop":
                visit(node.get("steps", []), owner)

    visit(steps, ROOT_FLOW)
    for name, definition in subflows.items():
        graph[name] = set()
        if isinstance(definition, dict):
            visit(definition.get("steps", []), name)
    return graph


def _check_subflow_calls(
    steps: Any, subflows: dict[str, Any], errors: list[str]
) -> None:
    graph = _subflow_graph(steps, subflows)
    for name, targets in graph.items():
        for target in targets:
            if target not in subflows:
                owner = "主流程" if name == ROOT_FLOW else f"子流程 {name}"
                errors.append(f"{owner}引用了不存在的 {target}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(name: str, depth: int, trail: list[str]) -> None:
        if name in visiting:
            errors.append(f"子流程存在循环调用：{' -> '.join([*trail, name])}")
            return
        if depth > 6:
            errors.append(f"子流程调用层级超过 6 层：{' -> '.join([*trail, name])}")
            return
        if name in visited:
            return
        visiting.add(name)
        for target in graph.get(name, set()):
            if target in subflows:
                walk(target, depth + 1, [*trail, name])
        visiting.remove(name)
        visited.add(name)

    for name in subflows:
        walk(name, 1, [])


def _contains_test(steps: Any) -> bool:
    if not isinstance(steps, list):
        return False
    for node in steps:
        if not isinstance(node, dict):
            continue
        node_type = node.get("type", "test")
        if node_type == "test":
            return True
        if node_type == "condition" and (_contains_test(node.get("then")) or _contains_test(node.get("else"))):
            return True
        if node_type == "parallel" and any(
            isinstance(branch, dict) and _contains_test(branch.get("steps"))
            for branch in node.get("branches", [])
        ):
            return True
        if node_type == "loop" and _contains_test(node.get("steps")):
            return True
    return False


def compile_control_script(source: dict[str, Any]) -> dict[str, Any]:
    script = deepcopy(source)
    errors: list[str] = []
    if not str(script.get("name", "")).strip():
        errors.append("脚本名称不能为空")
    if not str(script.get("product_model", "")).strip():
        errors.append("产品型号不能为空")
    if not isinstance(script.get("variables", {}), dict):
        errors.append("variables 必须是对象")
    steps = script.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("主流程至少需要一个节点")
    subflows = script.get("subflows", {})
    if not isinstance(subflows, dict):
        errors.append("subflows 必须是对象")
        subflows = {}
    ids: set[str] = set()
    counter = [0]
    _sequence(steps, "steps", errors, ids, 0, counter)
    for name, definition in subflows.items():
        if not ID_PATTERN.fullmatch(name):
            errors.append(f"子流程 ID 不合法：{name}")
        if not isinstance(definition, dict):
            errors.append(f"subflows.{name} 必须是对象")
            continue
        if not str(definition.get("name", "")).strip():
            errors.append(f"subflows.{name}.name 不能为空")
        _sequence(definition.get("steps"), f"subflows.{name}.steps", errors, ids, 0, counter)
    _check_subflow_calls(steps, subflows, errors)
    has_test = _contains_test(steps) or any(
        isinstance(definition, dict) and _contains_test(definition.get("steps"))
        for definition in subflows.values()
    )
    if not has_test:
        errors.append("主流程及子流程中至少需要一个 test 测试节点")
    if errors:
        raise ScriptValidationError(errors)
    script["schema_version"] = 2
    script["name"] = str(script["name"]).strip()
    script["product_model"] = str(script["product_model"]).strip()
    script.setdefault("variables", {})
    script.setdefault("subflows", {})
    return script
