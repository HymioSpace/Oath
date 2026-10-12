"""工具注册表。"""

from __future__ import annotations

import pytest

from core.protocol.error import ToolNotFoundError, ToolRegistrationError
from core.tool import ToolRegistry
from core.utils.encoding import JsonObject
from tests._support.tool_fixtures import EchoTool, StaticTool, definition


def test_registers_and_finds() -> None:
    registry = ToolRegistry()
    entry = registry.register(EchoTool("echo"))

    assert entry.name == "echo"
    assert registry.get("echo") is entry
    assert "echo" in registry
    assert len(registry) == 1


def test_duplicate_name_is_rejected() -> None:
    """同名注册是 bug。覆盖会让装配顺序决定行为 —— 那不是能接受的自由度。"""
    registry = ToolRegistry()
    registry.register(EchoTool("echo"))
    with pytest.raises(ToolRegistrationError):
        registry.register(EchoTool("echo"))
    assert len(registry) == 1


def test_get_returns_none_and_require_raises() -> None:
    registry = ToolRegistry()
    assert registry.get("nope") is None
    with pytest.raises(ToolNotFoundError):
        registry.require("nope")


@pytest.mark.parametrize(
    "name",
    ["Echo", "echo-1", "echo.one", "1echo", "echo tool", "", "_echo"],
)
def test_rejects_names_providers_would_refuse(name: str) -> None:
    """函数调用协议把名字直接拼进请求体，非法名字要死在装配期。"""
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError):
        registry.register(EchoTool(name))


def test_rejects_empty_description() -> None:
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError):
        registry.register(StaticTool(definition("echo", description="")))


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string"},
        {"properties": {}},
        "not-an-object",
        {"type": "object", "properties": 5},
        {"type": "object", "required": "a"},
    ],
)
def test_rejects_unusable_schema(schema: JsonObject) -> None:
    """不合 schema 的定义会让后面每次派发都炸，必须在装配期就挡住。"""
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError):
        registry.register(StaticTool(definition("echo", schema)))


def test_definitions_are_sorted_regardless_of_registration_order() -> None:
    """顺序稳定是硬要求：同一份工具集必须序列化成逐字节相同的请求体。"""
    names = ["zeta", "alpha", "mid", "beta"]
    forward = ToolRegistry()
    for name in names:
        forward.register(EchoTool(name))
    backward = ToolRegistry()
    for name in reversed(names):
        backward.register(EchoTool(name))

    assert [d.name for d in forward.definitions()] == ["alpha", "beta", "mid", "zeta"]
    assert forward.definitions() == backward.definitions()


def test_timeout_is_a_core_side_policy() -> None:
    registry = ToolRegistry(default_timeout_seconds=1.5)
    assert registry.register(EchoTool("a")).timeout_seconds == 1.5
    assert registry.register(EchoTool("b"), timeout_seconds=9.0).timeout_seconds == 9.0


@pytest.mark.parametrize("timeout", [0.0, -1.0])
def test_rejects_non_positive_timeouts(timeout: float) -> None:
    with pytest.raises(ToolRegistrationError):
        ToolRegistry(default_timeout_seconds=timeout)
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError):
        registry.register(EchoTool("a"), timeout_seconds=timeout)


def test_names_and_definitions_agree() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool("b"))
    registry.register(EchoTool("a"))
    assert registry.names() == ("a", "b")
    assert tuple(d.name for d in registry.definitions()) == registry.names()


def test_definition_is_the_single_source_of_truth() -> None:
    """工具的名字只从 definition 读一次，不许有第二个字段。"""
    registry = ToolRegistry()
    entry = registry.register(EchoTool("echo"))
    assert entry.name == entry.definition.name
    assert registry.definitions()[0].description == definition("echo").description
