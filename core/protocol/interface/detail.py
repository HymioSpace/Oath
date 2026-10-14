"""`DetailStore` —— 超阈值工具结果的全文存放处。

只存文本和取文本，没有查询、没有列举、没有删除。key 由 core 生成（当前是
`invocation_id`），实现方决定落在哪：文件、对象存储还是列。换个存储只换实现。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype import Scope


class DetailStore(ABC):
    """按 scope 隔离的全文存储。"""

    @abstractmethod
    def put(self, scope: Scope, key: str, content: str) -> None:
        """写入全文。同 key 覆盖。

        写失败抛异常：调用方据此降级成「不截断」，而不是留下一条指向空内容的记录。
        """
        ...

    @abstractmethod
    def get(self, scope: Scope, key: str) -> str | None:
        """取全文。取不到返回 `None` —— 包括 key 不存在和 scope 不匹配，两者不可区分。"""
        ...


__all__ = ["DetailStore"]
