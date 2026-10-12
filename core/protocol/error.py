"""契约层的异常，全部集中在这里。

项目约定：状态字段用于 tool_result / inbound_event 层面（工具超时、重复、结果
未知都是**值**不是异常）；领域不变量违反才用异常。协议层全部是不变量。
"""

from __future__ import annotations


class ProtocolError(Exception):
    """所有 core 契约层异常的基类。"""


class JournalError(ProtocolError):
    """journal 的全部失败。消费方一把 `except JournalError` 接住。"""


class ValidationError(JournalError):
    """调用方给的数据不合法 —— 补齐参数即可，不是状态问题。"""


class ScopeError(ValidationError):
    """`user_id` / `session_id` 不符合字符集，或与记录所在位置不一致。"""


class PayloadVersionError(ValidationError):
    """未知的 `payload_schema_version`。

    刻意不降级成「跳过这条记录」——静默丢弃等于丢数据。reader 必须先升级。
    """


class PayloadSchemaError(ValidationError):
    """payload 不符合该 `type` + version 的结构约束。"""


class CursorError(ValidationError):
    """游标种类不受本实现支持，或位置越界。"""


class IdempotencyKeyConflict(JournalError):
    """幂等键命中，但 payload 指纹不同。

    同一个键配不同内容是调用方的 bug。只做「键存在就返回原记录」会把
    这个 bug 变成静默丢记录：上层以为写进去了，实际拿到的是旧值。
    """


class CorruptRecordError(JournalError):
    """记录结构损坏，或记录 scope 与所在文件代表的 scope 不一致。"""


__all__ = [
    "CorruptRecordError",
    "CursorError",
    "IdempotencyKeyConflict",
    "JournalError",
    "PayloadSchemaError",
    "PayloadVersionError",
    "ProtocolError",
    "ScopeError",
    "ValidationError",
]
