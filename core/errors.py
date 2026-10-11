"""异常 —— 只用于「按契约不该发生」的情况，不用于表达业务失败。

数据面的失败（append 被拒、工具失败、事件非法）是状态字段，不用异常。
这里的异常一旦抛出，调用方应当按类型分别处理，而不是「重试看看」：

- 越权访问（ScopeViolation）
- 存储端拒绝旧 owner（FencedOut）
- 状态版本冲突（StateVersionConflict）
- 日志损坏（Corruption 及其子类）
- 输入结构不合法（ValidationError）
"""
from __future__ import annotations


class OathError(Exception):
    """本项目所有异常的根。调用方可以只捕获这一个来兜底。"""


# ---------- 隔离与授权 ----------

class ScopeViolation(OathError):
    """访问了 (user_id, session_id) 之外的资源。

    feature07 的隔离落在存储结构上：读写都按 scope 定位，同名 session 跨用户完全
    隔离。这条异常意味着隔离被绕过。
    """


# ---------- 并发控制 ----------

class FencedOut(OathError):
    """提交带的 owner_epoch 不是当前 epoch —— 这是个已经失去 owner 身份的写者。

    feature03：租约防止旧 owner 继续写。注意 fencing 拦不住已经发出去的外部请求，
    所以工具侧的幂等与对账依然必需。
    """

    def __init__(self, message: str, *, expected_epoch: int, actual_epoch: int) -> None:
        super().__init__(message)
        self.expected_epoch = expected_epoch
        self.actual_epoch = actual_epoch


class StateVersionConflict(OathError):
    """expected_state_version 与存储端不符 —— 另一个 state 提交抢先落地了。

    与 FencedOut 分开是因为处理方式不同：版本冲突可以重读 head 后重试，
    fencing 冲突必须放弃（当前进程已经不是 owner 了）。
    """

    def __init__(self, message: str, *, expected: int, actual: int) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual


# ---------- 数据完整性 ----------

class Corruption(OathError):
    """持久化数据不可信。"""


class LogCorrupt(Corruption):
    """日志内容损坏（seq 不连续 / 解析失败 / 同一 commit 出现两次且内容不同）。

    不做「读不出来就跳过」：feature03 要求损坏时停止该 session 并报告，而不是猜。
    """


class CursorInvalid(OathError):
    """cursor 无效，或与当前 principal / scope / 过滤条件不匹配。

    feature06 要求「旧 cursor 已不可用时显式返回错误而非跳过事件」，所以宁可报错
    也不能将就着继续读。
    """


# ---------- 输入与契约 ----------

class ValidationError(OathError):
    """记录结构不合法。

    一批里任一条不合法则整批拒绝，不部分写入（feature07：所有记录要么全部可见，
    要么全部不可见）。
    """


class LimitExceeded(OathError):
    """超过部署限制：记录大小、批次条数、页大小、类型白名单。

    与 ValidationError 分开是因为处理方式不同：feature07 要求满载时显式拒绝，
    「不静默丢记录」。
    """


class InjectedCrash(BaseException):
    """故障注入：模拟进程在提交写到一半时死掉。

    继承 BaseException 而非 Exception，是为了不让 runtime 顺手把它当普通错误处理掉 ——
    否则崩溃测试会变成普通异常处理路径，测不到恢复行为。
    只在测试中抛出。
    """
