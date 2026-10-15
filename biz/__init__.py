"""面向客户端的操作与查询 API。

传输无关：WebSocket、HTTP、命令行都调 `AgentApi`。进程是传输的事，这一层只管
「收到一个 command，runtime 变成什么样」。

```
biz/
├── agent.py   AgentApi —— 会话绑定与 command 接纳
├── query.py   只读 command 的分派与 session.status 读模型
└── trace.py   trace 列表与单条 trace 的读模型
```

依赖方向单向 —— `biz` → `bootstrap`（拿装配好的 `Runtime`）→ `core`。
core 不反向 import 这里任何东西。
"""

from __future__ import annotations

from biz.agent import AgentApi

__all__ = ["AgentApi"]
