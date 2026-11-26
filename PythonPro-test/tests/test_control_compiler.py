from __future__ import annotations

import pytest

from testsystem.compiler import ScriptValidationError, compile_script


def base_test_node(node_id: str) -> dict:
    return {
        "type": "test",
        "id": node_id,
        "name": "基础测试",
        "protocol": "simulator",
        "on_fail": "stop",
        "actions": [{"type": "sleep", "duration_ms": 0}],
    }


def definition(steps: list[dict], subflows: dict | None = None) -> dict:
    return {
        "schema_version": 2,
        "name": "引用校验",
        "product_model": "TEST-MODEL",
        "variables": {},
        "steps": steps,
        "subflows": subflows or {},
    }


def test_main_flow_rejects_missing_subflow() -> None:
    source = definition([
        {
            "type": "subflow",
            "id": "call_missing",
            "name": "调用缺失子流程",
            "subflow": "missing",
            "inputs": {},
            "on_fail": "stop",
        },
        base_test_node("fallback_test"),
    ])

    with pytest.raises(ScriptValidationError, match="主流程引用了不存在的 missing"):
        compile_script(source)


def test_main_flow_accepts_existing_subflow() -> None:
    source = definition(
        [
            {
                "type": "subflow",
                "id": "call_main",
                "name": "调用现有子流程",
                "subflow": "main",
                "inputs": {},
                "on_fail": "stop",
            }
        ],
        {"main": {"name": "现有子流程", "steps": [base_test_node("child_test")]}},
    )

    compiled = compile_script(source)

    assert compiled["schema_version"] == 2
    assert compiled["steps"][0]["subflow"] == "main"
