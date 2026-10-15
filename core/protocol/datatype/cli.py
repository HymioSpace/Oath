"""CLI 命令的声明形状。

命令的实现是扩展目录里的一个纯函数；`CliCommand` 是它在启动时被扫出来的那份声明 ——
名字、说明、参数 Schema、执行入口。路由、鉴权与错误回法不在这里。

类型放在 `core` 而不是 `biz`：扩展要声明命令就得能点名这个类型，而扩展不能 import
`biz`（`biz` 本来就 import 扩展，反过来就是环）。它与 `ToolDefinition` 是同一类东西 ——
带 JSON Schema 的纯数据声明。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from core.utils.encoding import JsonObject, JsonValue

#: 命令名的合法字符集。**命令名会进 shell 命令行**：模型把 `oath-cli <名字>` 原样打进
#: bash 执行环境的命令。名字来自扩展目录，而扩展目录可以是不进 git 的本地目录 ——
#: 仓库作者不再是唯一的把关人，所以字符集在这里收死，不靠下游转义。
COMMAND_NAME_PATTERN: Final = r"^[a-z][a-z0-9_-]{0,63}$"

_COMMAND_NAME_RE: Final = re.compile(COMMAND_NAME_PATTERN)


def is_valid_command_name(value: str) -> bool:
    """判断命令名能否安全地出现在命令行里。"""
    return _COMMAND_NAME_RE.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class CliCommand:
    """一条命令：它叫什么、要什么参数、谁来执行。

    `parameters` 是一份 JSON Schema，**属性声明的顺序就是位置参数顺序** ——
    `oath-cli echo hymio` 里 `hymio` 落在 `properties` 的第一项上。同一份声明也用来
    校验服务端收到的 body，不另立一份。

    `description` 与参数说明用英文：它们随请求发给调用方，是字面量不是文档。
    """

    name: str
    description: str
    parameters: JsonObject
    handler: Callable[..., JsonValue]


__all__ = ["COMMAND_NAME_PATTERN", "CliCommand", "is_valid_command_name"]
