"""业务工具扩展：注册得进去、跑得起来、失败抛得对。

这两个工具是扩展作者的模板，所以除了「能用」还要证明它们满足 core 的契约 ——
定义合法、入参经 schema 校验、业务失败抛 `ToolExecutionError`。
"""

from __future__ import annotations

from core.protocol.datatype import Scope, ToolCallRequest, ToolStatus
from core.tool import ToolRegistry
from core.tool.impl import ThreadedToolSystem
from core.utils.encoding import canonical_json, parse_timestamp
from extensions.tools import ClockTool, TextTemplateTool
from tests._support.tool_fixtures import RecordingSink, permission_manager, wait_terminal

SCOPE = Scope(user_id="u1", session_id="s1")


def build() -> tuple[ThreadedToolSystem, ToolRegistry, RecordingSink]:
    registry = ToolRegistry()
    sink = RecordingSink()
    registry.register(ClockTool(), timeout_seconds=5.0)
    registry.register(TextTemplateTool(), timeout_seconds=5.0)
    return ThreadedToolSystem(registry, sink, permission_manager()), registry, sink


def test_both_tools_register_and_appear_in_the_summary() -> None:
    system, registry, _ = build()
    try:
        assert registry.names() == ("current_time", "render_template")
        definitions = system.definitions()
        assert [d.name for d in definitions] == ["current_time", "render_template"]
        assert all(d.description for d in definitions)
        assert all(d.input_schema["type"] == "object" for d in definitions)
    finally:
        system.shutdown(wait=False)


def test_clock_returns_a_parsable_utc_timestamp() -> None:
    system, _, _ = build()
    try:
        system.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id="tc-c1", tool="current_time", arguments={}, invocation_id="c1"
                )
            ],
        )
        execution = wait_terminal(system, SCOPE, "c1")

        assert execution.status == ToolStatus.SUCCEEDED
        assert execution.result is not None
        assert isinstance(execution.result.content, str)
        moment = parse_timestamp(execution.result.content)
        assert moment.tzinfo is not None
    finally:
        system.shutdown(wait=False)


def test_clock_rejects_arguments_it_never_declared() -> None:
    """`additionalProperties: false` 让模型编出来的参数在接纳阶段就被挡掉。"""
    system, _, _ = build()
    try:
        handle = system.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id="tc-c2",
                    tool="current_time",
                    arguments={"tz": "UTC"},
                    invocation_id="c2",
                )
            ],
        ).handles[0]
        assert handle.state == "rejected"
    finally:
        system.shutdown(wait=False)


def test_template_renders_placeholders() -> None:
    system, _, _ = build()
    try:
        system.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id="tc-t1",
                    tool="render_template",
                    arguments={
                        "template": "你好，{{name}}。今天是 {{ day }}。",
                        "values": {"name": "小明", "day": "周一"},
                    },
                    invocation_id="t1",
                )
            ],
        )
        execution = wait_terminal(system, SCOPE, "t1")
        assert execution.result is not None
        assert execution.result.content == "你好，小明。今天是 周一。"
    finally:
        system.shutdown(wait=False)


def test_template_renders_non_string_values_as_json() -> None:
    """走 canonical JSON 而不是 `str()` —— 后者给布尔写成 True。"""
    system, _, _ = build()
    try:
        system.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id="tc-t2",
                    tool="render_template",
                    arguments={"template": "{{v}}", "values": {"v": {"a": [1, True]}}},
                    invocation_id="t2",
                )
            ],
        )
        execution = wait_terminal(system, SCOPE, "t2")
        assert execution.result is not None
        assert execution.result.content == canonical_json({"a": [1, True]})
    finally:
        system.shutdown(wait=False)


def test_template_tolerates_spaces_inside_placeholders() -> None:
    system, _, _ = build()
    try:
        system.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id="tc-t3",
                    tool="render_template",
                    arguments={"template": "{{ name }}", "values": {"name": "x"}},
                    invocation_id="t3",
                )
            ],
        )
        rendered = wait_terminal(system, SCOPE, "t3").result
        assert rendered is not None
        assert rendered.content == "x"
    finally:
        system.shutdown(wait=False)


def test_missing_placeholder_is_a_tool_error_not_a_blank_field() -> None:
    """缺失就报错。渲染成空串会让一个 bug 看起来像一个故意留白的字段。"""
    system, _, sink = build()
    try:
        system.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id="tc-t4",
                    tool="render_template",
                    arguments={"template": "{{a}}/{{b}}", "values": {"a": "1"}},
                    invocation_id="t4",
                )
            ],
        )
        execution = wait_terminal(system, SCOPE, "t4")

        assert execution.status == ToolStatus.FAILED
        assert execution.error is not None
        assert execution.error.kind == "tool_error"
        assert sink.wait_for(1)[0].payload["outcome"] == "failed"
    finally:
        system.shutdown(wait=False)
