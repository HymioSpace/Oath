"""core 的契约层：datatype、interface、error。

跨仓库的约定，被 Hymio、客户端、Nostalgia 按同一套形状消费，所以必须与任何
具体实现解耦：有 dataclass、有 Protocol、有 TypedDict、有异常，没有 I/O。
编码与时间处理在同级的 `core/utils/`，不属于契约。

本层不重导出任何符号 —— 多一条 import 路径就多一种拿法。消费方从三个包入口
import，不碰子模块：

    from core.protocol.datatype import User, AgentState
    from core.protocol.error import JournalError
    from core.protocol.interface import Journal
"""

from __future__ import annotations
