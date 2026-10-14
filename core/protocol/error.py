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


class ToolError(ProtocolError):
    """工具系统的全部失败。与 `JournalError` 平级，各自一把抓。"""


class ToolRegistrationError(ToolError):
    """工具定义不合法，或同名工具重复注册。

    注册期就抛：配错了工具要早暴露，远好过在模型第一次点名时才失败。
    """


class ToolNotFoundError(ToolError):
    """按名取工具但注册表里没有。"""


class ToolRequestError(ToolError):
    """整批派发请求本身不合法（空批次等），不针对某个具体的项。"""


class ArgumentValidationError(ToolError):
    """入参不符合工具定义的 `input_schema`。"""


class PermissionDeniedError(ToolError):
    """权限这一步没能得出结论。

    **不是**「策略判定为拒绝」—— 那是一个正常返回值 `PermissionVerdict.DENY`，
    不是异常。抛这个说明策略实现自己出了问题（读不到配置、连不上策略服务）。
    core 按 fail closed 处理：当成拒绝，不放行。宁可误杀不可误放。
    """


class PermissionError(ProtocolError):
    """权限系统的全部失败。与 `ToolError` 平级，各自一把抓。"""


class PermissionStoreError(PermissionError):
    """权限存储坏了或被写坏了（重复的待审批键、损坏的行、scope 对不上）。"""


class ToolExecutionError(ToolError):
    """扩展抛的：工具的业务失败。

    工具正常返回就等于成功；要表示失败就抛这个。core 把它记成 `FAILED`。
    """


class QueueError(ProtocolError):
    """队列的全部失败。与 `JournalError` 平级。"""


class UnknownQueueError(QueueError):
    """队列名不认识。

    与「读到一条不认识 type 的记录」不同：那种情况只是没命中，静默跳过就够；
    名字不认识是调用方的 bug，要当场暴露。
    """


class LlmError(ProtocolError):
    """模型调用的全部失败：网络、超时、非 2xx、响应体形状不对。

    没有「返回值表示失败」的形态 —— 一个 `LlmResponse` 一定是模型真说过的话。
    与 `ToolError` 平级：调用方要分别处理「模型没说话」与「工具没跑成」。
    """


class WireProtocolError(ProtocolError):
    """对端发来的帧不合法。

    **不是 Oath 内部的状态问题** —— 数据是外部进来的。带 `code` 是因为客户端
    需要区分「版本不对（该重新协商）」和「字段不对（该修客户端）」。
    """

    def __init__(self, message: str, *, code: str = "bad_frame") -> None:
        super().__init__(message)
        self.code = code


#: 重试也不会变好的失败。区别在于**换一次、原地再来一次**结果是不是同一个：
#: 这些说的是「这份输入本身不对」，不是「现在不行」。其余的（磁盘、网络、第三方
#: 服务）过一会儿再试可能就成了。
_PERMANENT_ERRORS: tuple[type[ProtocolError], ...] = (
    ValidationError,
    IdempotencyKeyConflict,
    CorruptRecordError,
    WireProtocolError,
)


def is_permanent(exc: BaseException) -> bool:
    """这个失败重试有没有意义。给「拿到一批输入去处理」的调用方分类用。"""
    return isinstance(exc, _PERMANENT_ERRORS)


__all__ = [
    "ArgumentValidationError",
    "CorruptRecordError",
    "CursorError",
    "QueueError",
    "IdempotencyKeyConflict",
    "JournalError",
    "LlmError",
    "PayloadSchemaError",
    "PayloadVersionError",
    "PermissionDeniedError",
    "PermissionError",
    "PermissionStoreError",
    "ProtocolError",
    "ScopeError",
    "ToolError",
    "ToolExecutionError",
    "ToolNotFoundError",
    "ToolRegistrationError",
    "ToolRequestError",
    "UnknownQueueError",
    "ValidationError",
    "is_permanent",
    "WireProtocolError",
]
