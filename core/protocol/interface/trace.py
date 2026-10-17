"""`TraceStore` —— span 的存放处。

trace 是 user 维度的，所以每个方法都要 `User`。存哪由实现方决定：文件、数据库，
或者直接送进 OTLP collector。core 只管写和读，不管落在哪。

只追加。一个 step 的 span 会写两次（开始一次、终态一次），同一个 `span_id` 最后
一条赢 —— 接口不需要 update，读的人按 id 去重。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype.trace import Span
from core.protocol.datatype.user import User


class TraceStore(ABC):
    """按 user 隔离的 span 追加日志。"""

    @abstractmethod
    def append(self, user: User, span: Span) -> None:
        """追加一条 span。同一个 span_id 追加多次时，最后一条赢。

        写失败抛异常：调用方据此丢掉这一条 span 继续跑 —— trace 是派生数据，
        不该因为写不进去就让推理停下来。
        """
        ...

    @abstractmethod
    def trace_ids(self, user: User) -> tuple[str, ...]:
        """这个 user 里有哪些 trace，**最近写入的排前面**。没有就返回空元组。"""
        ...

    @abstractmethod
    def spans(self, user: User, trace_id: str) -> tuple[Span, ...]:
        """一个 trace 的全部 span，按开始时间排序；同一个 span_id 只保留最后一条。

        trace_id 不存在就是空元组 —— 与 user 不匹配不可区分。
        """
        ...


__all__ = ["TraceStore"]
