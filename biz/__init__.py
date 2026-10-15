"""biz 层：runtime 需要对外暴露的功能，写在这里。

不放协议，不放生命周期 —— command 的接纳、事件的投递、身份的绑定都在 `bootstrap`。
biz 只负责把 runtime 手里的数据取出来给上层用，当前是 trace。

```
biz/
└── trace/     按 trace_id 取回一条 trace
```

一个能力一个子目录。依赖方向单向 —— `biz` → `bootstrap`（拿装配好的 `Runtime`）
→ `core`，core 不反向 import 这里任何东西。
"""

from __future__ import annotations

__all__: list[str] = []
