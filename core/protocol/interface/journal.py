"""`Journal` 接口。

模块边界，不是抽象基类：实现不继承它，只是形状一致。v1 只有 JSONL 一个
实现，所以没有基类、没有工厂、没有插件加载器。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from core.protocol.datatype import (
    AgentState,
    AppendResult,
    Cursor,
    Record,
    Recovery,
    Scope,
    StateRef,
)
from core.utils.encoding import JsonObject


class Journal(ABC):
    """统一追加日志。

    agent、工具、队列、Supervisor、biz 和扩展都在自己的流程里直接写记录，
    无需经主 agent 转发。
    """

    @property
    @abstractmethod
    def cursor_kind(self) -> str:
        """本实现的游标种类（`CursorKind`）。

        队列复位时要用它构造「第一条之前」的位置。写死一个 kind 会让换存储
        实现时读出错的区间，而这种错误不报错、只是安静地读错数据。
        """
        ...

    @abstractmethod
    def append(
        self,
        scope: Scope,
        type: str,
        payload: JsonObject,
        *,
        payload_schema_version: int,
        idempotency_key: str,
        occur_dt: str | None = None,
    ) -> AppendResult:
        """追加一条记录。

        幂等键在 `(user_id, session_id)` 内唯一。重复调用时：

        - 指纹（`type` + 版本 + payload 的 canonical hash）相同 → 视为重放，
          返回原记录，`idempotent_hit=True`，**不追加新行**。
        - 指纹不同 → 抛 `IdempotencyKeyConflict`，不覆盖也不追加。

        返回成功即代表记录已持久化，重启必可读。持久化失败时抛错，调用方
        不得 ACK。
        """
        ...

    @abstractmethod
    def commit_state(self, state: AgentState) -> StateRef:
        """提交一份 agent state 全量快照。

        `scope`、覆盖区间、`payload` 和 `payload_schema_version` 全都是 `state`
        自己的成员，不是入参：交回去的状态和拿回来的状态说的是同一份内容、
        同一个版本、同一个区间、同一个 scope，调用方没法把它们写岔。

        不要求幂等键：快照是自描述的（自带 cursor 区间），重复提交只是多写
        一份快照，不破坏任何东西。调用方在崩溃后想知道上次提交成没成功，
        读回 `recover()` 即可。
        """
        ...

    @abstractmethod
    def recover(self, scope: Scope) -> Recovery:
        """读回自洽的 `(state, messages)` 与待处理尾部。

        快照是优化不是正确性来源：即使从未提交过快照，本方法也能靠全量
        重放给出结果，只是更慢。
        """
        ...

    @abstractmethod
    def read_range(
        self, scope: Scope, cursor: Cursor, *, limit: int | None = None
    ) -> Iterator[Record]:
        """从 `cursor` **之后**（不含该位置）向后顺序读原始记录。

        不重建状态。给 trace 视图、审计、Supervisor 扫描用。
        """
        ...


__all__ = ["Journal"]
