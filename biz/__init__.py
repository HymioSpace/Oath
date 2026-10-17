"""biz 层：runtime 需要对外暴露的功能，写在这里。

不放协议，不放生命周期 —— command 的接纳、事件的投递、身份的绑定都在 `bootstrap`。
biz 只负责把 runtime 手里的数据取出来给上层用，当前是 trace 查询与 CLI 代理。

```
biz/
├── session_scope.py  从请求认出 user、认证与参数错误的回法
├── trace/            按 trace_id 取回一条 trace
└── cli/              业务 CLI 的命令表与路由
```

一个能力一个子目录。依赖方向单向 —— `bootstrap` → `biz` → `core`，core 不反向
import 这里任何东西。
"""

from __future__ import annotations

__all__: list[str] = []
