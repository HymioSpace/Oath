"""journal 的机制层：校验、幂等语义、游标运算、messages 投影。

契约在 `core/protocol/`，存储后端在 `core/journal/impl/`。v1 只有 JSONL 一个
实现，所以没有工厂、没有插件加载器。

依赖方向：本模块依赖 `core/protocol`，反过来没有。

`__init__` 只重导出机制，不转发契约层的符号 —— 契约的 import 走
`core.protocol.datatype` / `core.protocol.interface`。
"""

from __future__ import annotations

from core.journal.cursor import beginning, record_fingerprint
from core.journal.messages import project as project_messages

__all__ = [
    "beginning",
    "project_messages",
    "record_fingerprint",
]
