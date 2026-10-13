"""core 往外送的事件的共同形状。

不是一个枚举，也不是一个「什么都能装」的袋子：它是 `ToolEvent` 与
`PermissionEvent` 的基类，只承载**所有事件都要**的那几个字段 —— 谁发生的
（`scope`）、对应哪一次工具调用（两个 id）、什么时候、幂等键、以及已经成形
的 `payload`。

分工：`payload` 是各子类自己的落盘形态（`tool.result` 的 payload、审批记录的
payload），形状不同，**不能**上提到基类。基类只保证投递方（`AgentEventSink`）
拿到事件时不必先判断它是什么类型就能做通用的事：按 `scope` 分流、按
`idempotency_key` 去重、按 `occur_dt` 排序。

新增事件类型时加子类，不要往基类上加字段 —— 基类一改，所有已发出去的事件
形状就都变了。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.protocol.datatype.scope import Scope
from core.utils.encoding import JsonObject


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """core 产生、往外送的事件的基类。

    字段变更按破坏性变更处理：事件可能被下游落盘、转发、缓存。
    """

    #: 事件的 type，取各子类定义的闭合集合（`ToolEventType` /
    #: `PermissionEventType`）。基类不校验它 —— 不同领域的 type 命名空间不同。
    type: str
    scope: Scope
    #: 模型 API 给的调用标识，原样透传。Oath 不生成也不解释它。
    tool_call_id: str
    #: Oath 自己的调用标识：状态查询的键，也是事件之间配对的键。
    invocation_id: str
    #: 这次事件说的是哪个工具。审批事件也带 —— 批的是「这一次调这个工具」。
    tool: str
    #: 已经成形的落盘形态。形状由子类定义，基类不解释。
    payload: JsonObject
    #: 幂等键。含 `type` 前缀，所以两个事件类型不会撞。
    idempotency_key: str
    #: 墙钟时刻。**只用于展示与排查**，不参与任何判定 —— 墙钟会回拨。
    occur_dt: str
