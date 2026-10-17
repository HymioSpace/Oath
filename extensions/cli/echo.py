"""一条回显的命令 —— `oath-cli echo <文本>` 打到这里。

**它是 demo，不假装是业务能力。** 这套机制的形状是「命令是扩展目录里的纯函数、模块级
`COMMANDS` 声明它叫什么要什么参数、`oath-cli` 能在执行环境里调到它」；这条命令把每一环
都走一遍，够验证链路是不是通的。

**签名的后两个参数是框架统一给的**：`user` 是这次调用的身份，`user_context` 是客户端
随消息带来的那份 kv（已裁掉过期项）。用不上的命令也照收 —— 签名一致，要鉴权的命令才
能自己写在那儿，不必改框架。
"""

from __future__ import annotations

from typing import Final

from core.protocol.datatype import CliCommand
from core.protocol.datatype.user import User
from core.utils.encoding import JsonObject, JsonValue


def echo(text: str, *, user: User, user_context: JsonObject) -> JsonValue:
    """原样交回文本。"""
    return {"text": text}


#: 这条命令对外的样子。启动时扫出来，不改它就多不出第二条命令。
COMMANDS: Final[tuple[CliCommand, ...]] = (
    CliCommand(
        name="echo",
        description="Echo the text back.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文本"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        handler=echo,
    ),
)

__all__ = ["COMMANDS", "echo"]
