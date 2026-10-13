"""工具注册表：收集 core 与 extensions 的工具。

装配期构造一次，之后只读并发。超时是**core 侧的策略**（挂在注册项上），不是扩展
声明的属性 —— 扩展说「我跑多久」等于让扩展自己决定自己什么时候被放弃。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Final

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from core.protocol.datatype import ToolDefinition, is_valid_tool_name
from core.protocol.error import ToolNotFoundError, ToolRegistrationError
from core.protocol.interface import Tool

#: 默认单次执行超时。给业务工具留出跑一次外部调用的余量，又不至于让一个挂死的
#: 工具占着执行位不放手。具体值由装配期按工具覆盖。
DEFAULT_TIMEOUT_SECONDS: Final = 30.0


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """一个已注册的工具及其 core 侧策略。"""

    definition: ToolDefinition
    tool: Tool
    timeout_seconds: float

    @property
    def name(self) -> str:
        return self.definition.name


def _validate(definition: ToolDefinition) -> None:
    """注册期校验定义形状。

    校验在这里做而不是派发时：模型第一次点到这个名字要等到用户真的提了个请求，
    配错了工具应该在那之前就炸。
    """
    if not isinstance(definition, ToolDefinition):
        raise ToolRegistrationError(f"definition 必须是 ToolDefinition: {type(definition)!r}")
    if not is_valid_tool_name(definition.name):
        raise ToolRegistrationError(
            f"工具名不合法: {definition.name!r}，要求匹配 [a-z][a-z0-9_]{{0,63}}"
        )
    if not definition.description:
        raise ToolRegistrationError(f"工具 {definition.name} 的 description 为空")
    schema = definition.input_schema
    if not isinstance(schema, dict):
        raise ToolRegistrationError(
            f"工具 {definition.name} 的 input_schema 必须是 JSON 对象: {type(schema)!r}"
        )
    if schema.get("type") != "object":
        raise ToolRegistrationError(
            f"工具 {definition.name} 的 input_schema 顶层 type 必须是 object: "
            f"{schema.get('type')!r}"
        )
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ToolRegistrationError(
            f"工具 {definition.name} 的 input_schema 不合法: {exc}"
        ) from exc


class ToolRegistry:
    """按名字收集工具。"""

    def __init__(self, *, default_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        if default_timeout_seconds <= 0:
            raise ToolRegistrationError(f"默认超时必须为正: {default_timeout_seconds!r}")
        self._default_timeout = default_timeout_seconds
        self._entries: dict[str, RegisteredTool] = {}
        self._lock = threading.Lock()

    def register(self, tool: Tool, *, timeout_seconds: float | None = None) -> RegisteredTool:
        """注册一个工具。同名注册抛错，不覆盖 —— 覆盖会让装配顺序决定行为。

        同一份工具集必须在不同进程里产出相同的汇总，所以重复注册只能是 bug。
        """
        definition = tool.definition
        _validate(definition)
        timeout = self._default_timeout if timeout_seconds is None else timeout_seconds
        if timeout <= 0:
            raise ToolRegistrationError(f"工具 {definition.name} 的超时必须为正: {timeout!r}")
        entry = RegisteredTool(definition=definition, tool=tool, timeout_seconds=timeout)
        with self._lock:
            if definition.name in self._entries:
                raise ToolRegistrationError(f"工具名重复注册: {definition.name!r}")
            self._entries[definition.name] = entry
        return entry

    def get(self, name: str) -> RegisteredTool | None:
        with self._lock:
            return self._entries.get(name)

    def require(self, name: str) -> RegisteredTool:
        entry = self.get(name)
        if entry is None:
            raise ToolNotFoundError(f"工具未注册: {name!r}")
        return entry

    def names(self) -> tuple[str, ...]:
        """已注册的工具名，按字典序。"""
        with self._lock:
            return tuple(sorted(self._entries))

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """汇总给 context 的定义，按名字排序。

        排序不是美观问题：同一份工具集在不同进程、不同注册顺序下必须序列化成
        逐字节相同的请求体，否则 prompt 缓存、幂等指纹和 trace 全对不上。
        """
        with self._lock:
            entries = [self._entries[name] for name in sorted(self._entries)]
        return tuple(entry.definition for entry in entries)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        return self.get(name) is not None

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "RegisteredTool", "ToolRegistry"]
