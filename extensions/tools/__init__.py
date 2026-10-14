"""业务工具扩展。

参考实现，也是扩展作者的第一个模板：实现 `Tool`、在装配期注册进 `ToolRegistry`。
删掉这个包，runtime 仍能启动，只是没有业务能力 —— 判据见 `CLAUDE.md`。

这里刻意只有纯本地、无外部依赖的工具。能发 HTTP 请求的工具必须同时解决出网限制、
SSRF、响应体大小上限、重定向策略与超时，这些在权限策略成型之前写进仓库就是留洞。
"""

from __future__ import annotations

from core.protocol.interface import Tool
from extensions.tools.clock import ClockTool
from extensions.tools.demo import EchoTool
from extensions.tools.text_template import TextTemplateTool


def demo_tools() -> list[Tool]:
    """装配时注册的那一组。"""
    return [ClockTool(), EchoTool(), TextTemplateTool()]


__all__ = ["ClockTool", "EchoTool", "TextTemplateTool", "demo_tools"]
