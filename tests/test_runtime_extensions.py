"""扩展目录接进 runtime：扫到的东西真的进了模型能用的地方。

装配器自己的规则在 `test_extension_loader.py`。这里要验的是三件事各自接对了地方 ——
工具进了注册表、命令进了 CLI server 的那张表、技能进了 system prompt 的索引。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from bootstrap.biz_server import build_app
from bootstrap.runtime import Runtime
from core.protocol.datatype import ToolDefinition, ToolInvocation, ToolResult
from core.protocol.datatype.config import AgentConfig, OathConfig
from core.protocol.error import ToolRegistrationError
from core.protocol.interface import Tool
from tests._support.intake_fixtures import ScriptedProvider

TOKEN = "synthetic-test-token"

TOOL_SOURCE = """
from core.protocol.datatype import ToolDefinition, ToolInvocation, ToolResult
from core.protocol.interface import Tool
from core.utils.encoding import JsonObject

_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


class LocalTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="local_tool", description="A local tool.",
                              input_schema=_SCHEMA)

    def execute(self, invocation: ToolInvocation) -> ToolResult:
        return ToolResult(content=invocation.arguments.get("text"))
"""

COMMAND_SOURCE = """
from typing import Final

from core.protocol.datatype import CliCommand
from core.protocol.datatype.scope import Scope
from core.utils.encoding import JsonObject, JsonValue


def shout(text: str, *, scope: Scope, user_context: JsonObject) -> JsonValue:
    return {"text": text.upper()}


COMMANDS: Final[tuple[CliCommand, ...]] = (
    CliCommand(
        name="shout",
        description="Shout the text.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        handler=shout,
    ),
)
"""

SKILL = """---
name: local-skill
description: 开发者本机才有的技能。
---

正文。
"""


@pytest.fixture(autouse=True)
def _forget_loaded_modules() -> Iterator[None]:
    yield
    for name in [key for key in sys.modules if key.startswith("oath_ext_")]:
        del sys.modules[name]


@pytest.fixture
def local(tmp_path: Path) -> Path:
    """一个装齐三种扩展的本地目录。"""
    directory = tmp_path / "extensions-local"
    (directory / "tools").mkdir(parents=True)
    (directory / "tools" / "local_tool.py").write_text(TOOL_SOURCE, encoding="utf-8")
    (directory / "cli").mkdir()
    (directory / "cli" / "shout.py").write_text(COMMAND_SOURCE, encoding="utf-8")
    (directory / "skills" / "local-skill").mkdir(parents=True)
    (directory / "skills" / "local-skill" / "SKILL.md").write_text(SKILL, encoding="utf-8")
    return directory


def tool_names(runtime: Runtime) -> set[str]:
    """模型看得见的工具名 —— 注册表的内容就是那张表。"""
    return {definition.name for definition in runtime.ctx.tools.definitions()}


def runtime_of(tmp_path: Path, dirs: tuple[Path, ...]) -> Runtime:
    return Runtime(
        OathConfig(
            data_root=tmp_path,
            credentials={TOKEN: ("u1", "s1")},
            agent=AgentConfig(
                extension_dirs=dirs,
                skill_dir=tmp_path / "nowhere",
            ),
        ),
        loop=asyncio.new_event_loop(),
        provider=ScriptedProvider(),
    )


def test_a_local_tool_reaches_the_registry(tmp_path: Path, local: Path) -> None:
    """注册表就是模型看得见的工具列表 —— 扫不到就等于没有。"""
    runtime = runtime_of(tmp_path, (local,))
    try:
        assert "local_tool" in tool_names(runtime)
    finally:
        runtime.close()


def test_the_core_tools_are_still_registered(tmp_path: Path, local: Path) -> None:
    """扩展是加法，不是替换。删掉 extensions-local 只该少东西，不该让 runtime 起不来。"""
    runtime = runtime_of(tmp_path, (local,))
    try:
        assert {"detail_read", "skill", "bash"} <= tool_names(runtime)
    finally:
        runtime.close()


def test_a_local_command_reaches_the_cli_server(tmp_path: Path, local: Path) -> None:
    """命令在扩展里声明，但它是 HTTP 能力 —— 要能从 biz server 调通。"""
    runtime = runtime_of(tmp_path, (local,))
    try:
        client = TestClient(build_app(runtime))

        assert [c["name"] for c in client.get("/cli_commands").json()["commands"]] == ["shout"]
        response = client.post(
            "/cli/shout",
            json={"text": "工作区"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        assert response.status_code == 200
        assert response.json() == {"text": "工作区"}
    finally:
        runtime.close()


def test_a_local_skill_reaches_the_system_prompt_index(tmp_path: Path, local: Path) -> None:
    """开发者自己写的技能是项目技能，进索引，不只是被 find 搜到。"""
    runtime = runtime_of(tmp_path, (local,))
    try:
        assert "local-skill" in runtime.ctx.skills.list(1000)
    finally:
        runtime.close()


def test_the_runtime_starts_with_the_scan_turned_off(tmp_path: Path) -> None:
    runtime = runtime_of(tmp_path, ())
    try:
        assert runtime.commands == ()
        assert runtime.ctx.skills.list(1000) == ""
    finally:
        runtime.close()


class Conflicting(Tool):
    """名字撞上扫出来的那条。"""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="local_tool", description="clash", input_schema={"type": "object"}
        )

    def execute(self, invocation: ToolInvocation) -> ToolResult:
        return ToolResult()


def test_an_injected_tool_that_clashes_with_a_discovered_one_is_refused(
    tmp_path: Path, local: Path
) -> None:
    """撞名要在装配期炸出来。装上了、模型调不动，那是更难查的坏。"""
    with pytest.raises(ToolRegistrationError):
        Runtime(
            OathConfig(
                data_root=tmp_path,
                agent=AgentConfig(extension_dirs=(local,), skill_dir=tmp_path / "nowhere"),
            ),
            loop=asyncio.new_event_loop(),
            provider=ScriptedProvider(),
            tools=[Conflicting()],
        )
