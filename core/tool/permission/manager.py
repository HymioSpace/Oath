"""权限管理器：判定 → 记录 → 发事件 → 定时过期。

三件事，各有各的失败方式，所以分开放：

1. **判定**调 `PermissionPolicy`，三值返回。策略抛异常就 fail closed —— 当成拒绝，
   不放行。宁可误杀不可误放。
2. **`ask` 的调用转成待审批**：立刻落一条记录、立刻发 `permission.requested` 事件、
   把有效期压进堆。**当场就发**，不等后面任何东西 —— 一个等人批的请求如果没被
   告知出去，就永远等不到。
3. **有效期到点由 watchdog 判过期**，发 `permission.resolved{status: expired}`。
   不是每次查询时比时间：那样一个没人再碰的请求会永远挂着。

本模块**不负责审批通过后的重新派发**。裁定结果一律发事件给 agent，怎么接着走
是 agent runtime 的事。
"""

from __future__ import annotations

import heapq
import itertools
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Final

from core.protocol.datatype import (
    PERMISSION_REQUESTED_KEY_PREFIX,
    PERMISSION_RESOLVED_KEY_PREFIX,
    PermissionEvent,
    PermissionEventType,
    PermissionRecord,
    PermissionStatus,
)
from core.protocol.datatype.permission import PermissionVerdict
from core.protocol.datatype.scope import Scope
from core.protocol.error import PermissionDeniedError, PermissionError
from core.protocol.interface import PermissionEventSink, PermissionPolicy
from core.tool.permission.store import PermissionStore
from core.utils.encoding import JsonObject, utc_now_iso

#: 审批请求的默认有效期。给够人看一眼的时间，又不至于让一个没人管的请求
#: 在系统里挂到天荒地老。
DEFAULT_TTL_SECONDS: Final = 300.0


@dataclass(order=True, slots=True)
class _Deadline:
    """watchdog 的堆项。按 `(deadline, seq)` 排序，seq 用来打破同刻的平局。"""

    deadline: float
    seq: int = field(compare=True)
    record: PermissionRecord = field(compare=False)


def _plus_seconds(seconds: float) -> str:
    """把秒数加到当前 UTC 时刻上，产出 RFC 3339 字符串。

    有效期是**墙钟**（要给人看、要和记录里的 `requested_dt` 对得上），
    而 watchdog 用 monotonic 时钟调度。两者分开是有意的：墙钟会回拨，
    所以它只用来展示和记录，真正决定过期的是 watchdog 那一刻醒来的时机。
    """
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class PermissionManager:
    """`PermissionPolicy` + 存储 + 事件出口 + watchdog。

    线程安全：`_lock` 保护堆与开关；事件投递在锁外做 —— sink 可能慢，
    甚至可能回调回本对象。
    """

    def __init__(
        self,
        store: PermissionStore,
        policy: PermissionPolicy,
        sink: PermissionEventSink,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._store = store
        self._policy = policy
        self._sink = sink
        self._ttl_seconds = _positive(ttl_seconds, "ttl_seconds")
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._heap: list[_Deadline] = []
        self._counter = itertools.count()
        self._stopped = False
        self._watchdog = threading.Thread(
            target=self._watch_loop, name="oath-permission-watchdog", daemon=True
        )
        self._watchdog.start()

    # ---------------------------------------------------------------- 查询

    def get(self, scope: Scope, invocation_id: str) -> PermissionRecord | None:
        return self._store.get(scope, invocation_id)

    def pending(self, scope: Scope) -> tuple[PermissionRecord, ...]:
        return self._store.pending(scope)

    # ---------------------------------------------------------------- 判定

    def authorize(
        self,
        scope: Scope,
        tool: str,
        tool_call_id: str,
        invocation_id: str,
        arguments: JsonObject,
    ) -> str:
        """判一次权限。返回 `PermissionVerdict` 的三值之一。

        `ask` 的分支已经把副作用都做完了（落记录、发事件、起定时器），所以
        调用方拿到 `ASK` 时可以放心地不管后续 —— 需要有人批这件事已经被通知出去了。
        """
        if self._stopped:
            raise PermissionDeniedError("权限系统已关闭")

        # 已经有记录就说明这次调用问过权限了。重发（模型重发、agent 重放、审批
        # 回来后重新派发）不该产生第二个审批请求 —— 那是两条互相矛盾的记录。
        # 这里也让存储顺带当了决策缓存：批过了就放行，不用再问一次策略。
        known = self._store.get(scope, invocation_id)
        if known is not None:
            return self._replay_verdict(known, tool, tool_call_id)

        try:
            verdict = self._policy.check(scope, tool, arguments)
        except PermissionDeniedError:
            raise
        except Exception as exc:
            # 策略自己出错了，不是「策略说不」。fail closed：转成拒绝，不放行。
            # 原始原因放进消息，别让调用方只看到一个没有线索的 DENY。
            raise PermissionDeniedError(
                f"权限策略异常，按拒绝处理（{type(exc).__name__}: {exc}）"
            ) from exc

        if verdict not in PermissionVerdict.ALL:
            raise PermissionError(f"权限策略返回了未知的判定 {verdict!r}")
        if verdict == PermissionVerdict.ASK:
            self._open_request(scope, tool, tool_call_id, invocation_id, arguments)
        return verdict

    # ---------------------------------------------------------------- 裁定

    def _replay_verdict(self, known: PermissionRecord, tool: str, tool_call_id: str) -> str:
        """同一 `invocation_id` 第二次来的时候，回放上次的结果。

        `APPROVED` 映射成 `ALLOW`：批过了就该跑，而且不该再去问策略一次 ——
        审批窗口里策略可能已经变了，而人批的是**这一次具体调用**。
        `EXPIRED` 映射成 `DENY`：过期就是没人批，不批就不能跑。
        """
        if known.tool != tool or known.tool_call_id != tool_call_id:
            raise PermissionError(
                f"invocation_id {known.invocation_id!r} 已经用在 "
                f"{known.tool}/{known.tool_call_id} 上，不能拿来调 {tool}/{tool_call_id}"
            )
        if known.status == PermissionStatus.APPROVED:
            return PermissionVerdict.ALLOW
        if known.status == PermissionStatus.PENDING:
            return PermissionVerdict.ASK
        return PermissionVerdict.DENY

    def decide(
        self,
        scope: Scope,
        invocation_id: str,
        status: str,
        *,
        decided_by: str | None = None,
        reason: str | None = None,
    ) -> bool:
        """外部（人或系统）给一条待审批请求下裁定。返回是否真的生效。

        已经有人定过的返回 `False` 且什么都不做 —— 两个审批人同时点「同意」，
        第二个人的点击必须无效，否则工具会被跑两次。
        """
        if status not in PermissionStatus.RESOLVED:
            raise PermissionError(f"decide 只接受已终结的状态，得到 {status!r}")
        known = self._store.get(scope, invocation_id)
        if known is None:
            return False
        if known.is_resolved:
            return False

        record = replace(
            known,
            status=status,
            decided_by=decided_by,
            decided_dt=utc_now_iso(),
            reason=reason,
        )
        if not self._store.resolve(record):
            # 存储说没生效：别人先一步定了。以存储为准，不发事件。
            return False
        self._emit(record, PermissionEventType.RESOLVED)
        return True

    def shutdown(self, *, wait: bool = True) -> None:
        with self._condition:
            if self._stopped:
                return
            self._stopped = True
            self._condition.notify_all()
        if wait:
            self._watchdog.join(timeout=5.0)

    # ---------------------------------------------------------------- 内部

    def _open_request(
        self,
        scope: Scope,
        tool: str,
        tool_call_id: str,
        invocation_id: str,
        arguments: JsonObject,
    ) -> PermissionRecord:
        """把一个 `ask` 的调用转成待审批请求。"""
        record = PermissionRecord(
            scope=scope,
            tool_call_id=tool_call_id,
            invocation_id=invocation_id,
            tool=tool,
            arguments=arguments,
            status=PermissionStatus.PENDING,
            requested_dt=utc_now_iso(),
            expires_dt=_plus_seconds(self._ttl_seconds),
        )
        self._store.record(record)
        # 先把堆里的位置占住再发事件：事件投递方可能立刻回一个裁定回来，
        # 那样 watchdog 醒来时能看到这个请求已经不需要再管。
        with self._condition:
            heapq.heappush(
                self._heap,
                _Deadline(time.monotonic() + self._ttl_seconds, next(self._counter), record),
            )
            self._condition.notify_all()
        self._emit(record, PermissionEventType.REQUESTED)
        return record

    def _emit(self, record: PermissionRecord, type_: str) -> None:
        prefix = (
            PERMISSION_REQUESTED_KEY_PREFIX
            if type_ == PermissionEventType.REQUESTED
            else PERMISSION_RESOLVED_KEY_PREFIX
        )
        event = PermissionEvent(
            type=type_,
            scope=record.scope,
            tool_call_id=record.tool_call_id,
            invocation_id=record.invocation_id,
            tool=record.tool,
            status=record.status,
            payload=record.payload(),
            idempotency_key=f"{prefix}{record.invocation_id}",
            occur_dt=record.decided_dt or record.requested_dt,
        )
        # 投递失败就抛，不吞：一条送不出去的审批请求等于凭空消失，而调用方
        # 永远不会知道有这么一次调用。这里不做重试 —— 送不出去的时候重试只会
        # 反复失败。
        self._sink.emit(event)

    # ---------------------------------------------------------------- 过期

    def _watch_loop(self) -> None:
        """到点就判过期。不查墙钟 —— 醒来那一刻就是「过点了」这个事实。"""
        while True:
            with self._condition:
                while True:
                    self._prune_locked()
                    if self._stopped:
                        return
                    if not self._heap:
                        self._condition.wait()
                        continue
                    remaining = self._heap[0].deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)
                expired = heapq.heappop(self._heap)
            self._expire(expired.record)

    def _prune_locked(self) -> None:
        """清掉堆里已终结的项。

        裁定掉一条请求不会把它从堆里摘出来，不清就是一个只增不减的集合 ——
        长期运行的进程里这就是一处慢性泄漏。每次醒来都重建一次。
        """
        if not self._heap:
            return
        self._heap = [item for item in self._heap if not item.record.is_resolved]
        heapq.heapify(self._heap)

    def _expire(self, record: PermissionRecord) -> None:
        """判过期。已有人定过的什么都不做 —— 终态不可被改写。"""
        latest = self._store.get(record.scope, record.invocation_id)
        if latest is None or latest.is_resolved:
            return
        expired = replace(
            latest,
            status=PermissionStatus.EXPIRED,
            decided_by=None,
            decided_dt=utc_now_iso(),
            reason=f"{self._ttl_seconds:g}s 内没有裁定",
        )
        if not self._store.resolve(expired):
            return
        self._emit(expired, PermissionEventType.RESOLVED)


def _positive(value: float, name: str) -> float:
    if value <= 0:
        raise PermissionError(f"{name} 必须为正: {value!r}")
    return value


__all__ = ["DEFAULT_TTL_SECONDS", "PermissionManager"]
