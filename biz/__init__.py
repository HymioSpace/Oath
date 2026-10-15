"""面向客户端的操作与查询 API。

传输无关：WebSocket、HTTP、命令行都调 `AgentApi`。进程是传输的事，这一层只管
「收到一个 command，runtime 变成什么样」。

```
biz/
├── api.py       AgentApi —— 门面与只读 command 的分派
├── session/     会话：身份绑定、输入接纳、状态投影
└── trace/       trace：列表与单条的读模型
```

按域分，与 `core/` 下的写法一致。依赖方向单向 —— `biz` → `bootstrap`（拿装配好的
`Runtime`）→ `core`，core 不反向 import 这里任何东西。
"""

from __future__ import annotations

from biz.api import AgentApi

__all__ = ["AgentApi"]
