"""业务 CLI 的命令表与路由。命令的实现在 `extensions/cli/`，两边互不 import 反向。"""

from biz.cli.cli_server import COMMANDS, CliCommand, cli_routes

__all__ = ["COMMANDS", "CliCommand", "cli_routes"]
