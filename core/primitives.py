"""跨模块共用的字面量取值。

用 `Literal` 而不是 `Enum`：这些值在记录里就是以字符串存盘的，`Enum` 会逼着每个
读取方做一次 `.value` 转换。

取值一旦公开就是协议的一部分：新增是兼容变更，删除是破坏性变更。
"""
from __future__ import annotations

from typing import Literal, TypeAlias

# ---------- 记录分类（feature07 §记录结构）----------
#
# 三类的写权限与消费方式不同：
#   event       —— 提交后即等待投递；后续由事件运行时路由到队列
#   state       —— 只有当前 session owner 能写；用于恢复
#   observation —— 任何模块可写；trace 与诊断，不影响执行状态
RecordCategory: TypeAlias = Literal["event", "state", "observation"]

# ---------- 逻辑队列（feature01 §队列表）----------
QueueName: TypeAlias = Literal["user", "tool", "system"]

QUEUE_NAMES: tuple[str, ...] = ("user", "tool", "system")

# ---------- 提交结果 ----------
AppendOutcome: TypeAlias = Literal[
    "accepted",
    "duplicate",
    "conflict",
    "backpressure",
    "unavailable",
]

#: 记录内容的版本。与「怎么折叠这些记录」的 reducer 版本是两回事，各自演进。
SCHEMA_VERSION: int = 1
