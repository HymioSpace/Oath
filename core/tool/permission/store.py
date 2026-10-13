"""审批记录的存储接口。

只存两件事：**谁在等批注**、**最后批成了什么**。业务状态不在这儿 —— 那是 journal
的事。这个存储记的是运行时协调状态：一个待审批的请求是一个活对象，它有有效期，
会被外部裁定，会自己过期。

写入是 append-only：请求一行、裁定一行，同一个 `invocation_id` 后者覆盖前者。
当前状态是打开时 fold 出来的内存索引。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype.permission import PermissionRecord
from core.protocol.datatype.scope import Scope


class PermissionStore(ABC):
    """审批记录的读写。实现只需要保证下面的语义，不保证长什么样。

    - **只保证「当前状态」，不保证历史。** 想追问「这个调用当时被判成什么、
      后来谁改的」，去读日志，别指望这个接口。
    - **裁定只生效一次。** 同一条请求被裁定两次，第二次返回 `False` 且不写盘。
      这是迟到裁定与重放下的唯一安全语义。
    - **跨 scope 查不到。** 用别的 session 去问一个 `invocation_id` 得到 `None`，
      不泄露它存不存在（对照 journal 修过的越权读）。
    """

    @abstractmethod
    def record(self, record: PermissionRecord) -> None:
        """记一条新的待审批请求。

        同一个 `(scope, invocation_id)` 已经有一条记录时抛 `PermissionStoreError` ——
        工具系统按 `invocation_id` 去重，走到这里说明去重漏了。
        """
        ...

    @abstractmethod
    def resolve(self, record: PermissionRecord) -> bool:
        """裁定一条待审批请求。

        `record.status` 必须是 `PermissionStatus.RESOLVED` 里的值。返回是否**真的**
        改动了状态：已经被裁定过的返回 `False` 且不写盘。
        """
        ...

    @abstractmethod
    def get(self, scope: Scope, invocation_id: str) -> PermissionRecord | None:
        """查一条请求的当前状态。查不到返回 `None` —— 这是查询，不是命令。"""
        ...

    @abstractmethod
    def pending(self, scope: Scope) -> tuple[PermissionRecord, ...]:
        """全部还在等裁定的请求。顺序稳定（按写入顺序），便于断言与展示。"""
        ...


__all__ = ["PermissionStore"]
