"""`bash` —— 在执行环境里跑一条命令，以及 system prompt 里的 workspace 段落。

内置工具，不是扩展：执行环境与 workspace 是 core 自己的概念，扩展拿到的是「跑一条命令
并回结果」。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

from core.protocol.datatype import Scope, ToolDefinition, ToolInvocation, ToolResult
from core.protocol.error import ToolExecutionError
from core.protocol.interface.execution import ExecutionEnvironment
from core.protocol.interface.tool import Tool
from core.utils.encoding import JsonObject, canonical_json

#: 工具名。它只在这里出现一次。
NAME = "bash"

#: 没有 deadline 时命令能跑多久。有 deadline 时用剩下的时间 —— 排队已经吃掉的那部分
#: 不该再算给命令。
_DEFAULT_TIMEOUT_SECONDS: Final = 30.0

_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "要执行的命令，按 shell 语法解析"},
        "cwd": {"type": "string", "description": "工作目录；不传就是工作区"},
    },
    "required": ["command"],
    "additionalProperties": False,
}


def workspace_note(root: Path) -> str:
    """system prompt 里的工作区段落。

    写在 prompt 里是为了让模型知道默认在哪 —— 它不靠 bash 的参数说明去猜当前目录。
    """
    return f"## 工作区\n命令默认在 {root} 下执行。要去别处，给 bash 传 cwd。"


class BashTool(Tool):
    """执行环境外面那层：把命令与工作目录翻译成执行环境的一次调用。

    **命令环境不可用时才是失败**（抛 `ToolExecutionError`）。非零退出码、命令不存在、
    超时都不是 —— 那些是命令的结果，模型要读它们才知道发生了什么。
    """

    def __init__(
        self,
        env: ExecutionEnvironment,
        cli_env: Callable[[Scope], Mapping[str, str]],
        *,
        root: Path,
    ) -> None:
        self._env = env
        self._cli_env = cli_env
        self._root = root
        self._definition = ToolDefinition(
            name=NAME,
            description=(
                "在执行环境里跑一条命令并返回 exit_code、stdout、stderr 与耗时。"
                f"默认工作目录是 {root}。runtime 自己的命令行工具也在 PATH 上，"
                "用 oath-cli（不带参数可列出可用命令）调用。"
            ),
            input_schema=_SCHEMA,
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def execute(self, invocation: ToolInvocation) -> ToolResult:
        command = invocation.arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolExecutionError("command 必须是非空文本")
        cwd = invocation.arguments.get("cwd")
        try:
            result = self._env.run(
                command,
                cwd=Path(cwd) if isinstance(cwd, str) and cwd else self._root,
                timeout=self._timeout(invocation.deadline),
                env=self._cli_env(invocation.scope),
            )
        except OSError as exc:
            # 环境不可用，不是命令的错。
            raise ToolExecutionError(f"执行环境不可用：{exc}") from exc
        return ToolResult(
            canonical_json(
                {
                    "command": command,
                    "cwd": str(cwd or self._root),
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                    "timed_out": result.timed_out,
                    "truncated": result.truncated,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
        )

    def _timeout(self, deadline: float | None) -> float:
        if deadline is None:
            return _DEFAULT_TIMEOUT_SECONDS
        return max(0.0, deadline - time.monotonic())


__all__ = ["NAME", "BashTool", "workspace_note"]
