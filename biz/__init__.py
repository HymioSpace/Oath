"""面向客户端的操作与查询 API。

依赖方向单向 —— `biz` → `bootstrap` → `core`。core 不反向 import 这里任何东西。
"""

from __future__ import annotations

from biz.api import AgentApi

__all__ = ["AgentApi"]
