"""业务 CLI 的路由与代理。命令本身在扩展目录里声明，两边互不 import 反向。"""

from biz.cli.cli_server import cli_routes

__all__ = ["cli_routes"]
