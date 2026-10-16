"""Brain 只提供串行执行入口；唤醒条件属于各 consumer。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype.scope import Scope


class Brain(ABC):
    @abstractmethod
    def wake(self, scope: Scope) -> None:
        """非阻塞请求。buffer 空就什么都不做。"""
        ...

    @abstractmethod
    def resume(self, scope: Scope) -> None:
        """恢复未完成 step；失败态需用户显式 retry。"""
        ...

    @abstractmethod
    def retry(self, scope: Scope) -> None: ...

    @abstractmethod
    def close(self, *, wait: bool = True) -> None: ...
