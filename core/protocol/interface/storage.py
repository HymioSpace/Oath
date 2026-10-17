"""跨服务存储契约。调用方只提交数据，不传函数、SQL 或文件路径。"""

from typing import Any, Protocol


class StorageConflict(Exception):
    """版本冲突，纯状态变换可以重算。"""


class LeaseLost(Exception):
    """执行者已失去提交资格，不能重试副作用。"""


class StoragePort(Protocol):
    def call(self, operation: str, **params: Any) -> Any: ...
