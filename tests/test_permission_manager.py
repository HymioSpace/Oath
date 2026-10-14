"""`PermissionManager`：三值判定、审批请求、裁定、有效期过期。

定时相关的断言等的是「`permission.resolved` 事件真的到了」这个事实，不是一个
时长猜出来的窗口。对应 `spec/feature-tool-system.md` 的「验收与验证」。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from core.protocol.datatype import AgentEvent, Scope
from core.protocol.datatype.permission import (
    PermissionEventType,
    PermissionStatus,
    PermissionVerdict,
)
from core.protocol.error import PermissionDeniedError, PermissionError
from core.protocol.interface import PermissionPolicy
from core.tool.permission import PermissionManager, PermissionStore
from core.utils.encoding import JsonObject
from tests._support.tool_fixtures import (
    BrokenPolicy,
    MemoryPermissionStore,
    RecordingSink,
    VerdictPolicy,
)

SCOPE = Scope(user_id="u1", session_id="s1")

#: 足够长，让 watchdog 永远来不及在测试跑完之前动手。要它过期的测试自己传小值。
LONG_TTL = 300.0


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def store() -> MemoryPermissionStore:
    return MemoryPermissionStore()


def build(
    store: PermissionStore,
    sink: RecordingSink,
    policy: PermissionPolicy | None = None,
    *,
    ttl_seconds: float = LONG_TTL,
) -> PermissionManager:
    return PermissionManager(
        store, policy if policy is not None else VerdictPolicy(), sink, ttl_seconds=ttl_seconds
    )


@pytest.fixture
def manager(store: MemoryPermissionStore, sink: RecordingSink) -> Iterator[PermissionManager]:
    instance = build(store, sink)
    yield instance
    instance.shutdown(wait=False)


def waiting_policy() -> VerdictPolicy:
    return VerdictPolicy(default=PermissionVerdict.ASK)


# --------------------------------------------------------------- 三值判定


def test_allow_leaves_no_trace(manager: PermissionManager, sink: RecordingSink) -> None:
    """放行是当场结论，没有要等的人，所以不留痕。"""
    assert manager.authorize(SCOPE, "echo", "tc-1", "inv-1", {}) == PermissionVerdict.ALLOW
    assert manager.pending(SCOPE) == ()
    assert sink.events == []


def test_deny_leaves_no_trace(store: MemoryPermissionStore) -> None:
    """拒绝也是当场结论：没有要等的人，所以不留痕。"""
    sink = RecordingSink()
    manager = build(store, sink, VerdictPolicy({"echo": PermissionVerdict.DENY}))
    try:
        assert manager.authorize(SCOPE, "echo", "tc-1", "inv-1", {}) == PermissionVerdict.DENY
        assert manager.pending(SCOPE) == ()
        assert sink.events == []
    finally:
        manager.shutdown(wait=False)


def test_ask_opens_a_request_and_emits_immediately(
    store: MemoryPermissionStore, sink: RecordingSink
) -> None:
    """`ask` 的副作用当场做完：调用方拿到 ASK 时该发的已经发出去了。

    一个等人批的请求如果没被告知出去，就永远等不到。
    """
    manager = build(store, sink, waiting_policy())
    try:
        verdict = manager.authorize(SCOPE, "rm", "tc-1", "inv-1", {"path": "/"})
        assert verdict == PermissionVerdict.ASK

        record = manager.get(SCOPE, "inv-1")
        assert record is not None
        assert record.status == PermissionStatus.PENDING
        assert record.arguments == {"path": "/"}
        assert record.expires_dt > record.requested_dt

        requested = sink.wait_for_permission(PermissionEventType.REQUESTED)
        assert len(requested) == 1
        assert requested[0].invocation_id == "inv-1"
        assert requested[0].tool_call_id == "tc-1"
        assert requested[0].status == PermissionStatus.PENDING
        assert requested[0].idempotency_key.endswith("inv-1")
    finally:
        manager.shutdown(wait=False)


def test_a_broken_policy_fails_closed(store: MemoryPermissionStore, sink: RecordingSink) -> None:
    """策略自己抛错 = 没得出结论。按拒绝处理，不放行。宁可误杀不可误放。"""
    manager = build(store, sink, BrokenPolicy())
    try:
        with pytest.raises(PermissionDeniedError):
            manager.authorize(SCOPE, "echo", "tc-1", "inv-1", {})
        assert sink.events == []
    finally:
        manager.shutdown(wait=False)


def test_an_unknown_verdict_raises(store: MemoryPermissionStore) -> None:
    """策略返回了一个没约定的值。这是接口违约，不能猜成哪个。"""
    sink = RecordingSink()
    manager = build(store, sink, VerdictPolicy({"echo": "maybe"}))
    try:
        with pytest.raises(PermissionError):
            manager.authorize(SCOPE, "echo", "tc-1", "inv-1", {})
    finally:
        manager.shutdown(wait=False)


def test_a_closed_manager_denies_everything(manager: PermissionManager) -> None:
    manager.shutdown(wait=False)
    with pytest.raises(PermissionDeniedError):
        manager.authorize(SCOPE, "echo", "tc-1", "inv-1", {})


# --------------------------------------------------------------- 回放


def test_an_approved_request_replays_as_allow(
    store: MemoryPermissionStore, sink: RecordingSink
) -> None:
    """批过了就放行，且不再问一次策略 —— 人批的是**这一次具体调用**。

    审批窗口里策略可能已经变了，用此刻的策略翻案人的裁定是不对的。
    """
    manager = build(store, sink, waiting_policy())
    try:
        manager.authorize(SCOPE, "rm", "tc-1", "inv-1", {})
        assert manager.decide(SCOPE, "inv-1", PermissionStatus.APPROVED) is True
        assert manager.authorize(SCOPE, "rm", "tc-1", "inv-1", {}) == PermissionVerdict.ALLOW
        assert len(sink.permission_of_type(PermissionEventType.REQUESTED)) == 1
    finally:
        manager.shutdown(wait=False)


def test_a_pending_request_replays_as_ask(
    store: MemoryPermissionStore, sink: RecordingSink
) -> None:
    """重发不该产生第二个审批请求 —— 那是两条互相矛盾的记录。"""
    manager = build(store, sink, waiting_policy())
    try:
        assert manager.authorize(SCOPE, "rm", "tc-1", "inv-1", {}) == PermissionVerdict.ASK
        assert manager.authorize(SCOPE, "rm", "tc-1", "inv-1", {}) == PermissionVerdict.ASK
        assert len(sink.permission_of_type(PermissionEventType.REQUESTED)) == 1
        assert len(manager.pending(SCOPE)) == 1
    finally:
        manager.shutdown(wait=False)


def test_a_resolved_request_replays_as_deny(
    store: MemoryPermissionStore, sink: RecordingSink
) -> None:
    """被拒或已过期的重发一律判成拒绝：没批就是不能跑。"""
    manager = build(store, sink, waiting_policy())
    try:
        manager.authorize(SCOPE, "rm", "tc-1", "inv-1", {})
        manager.decide(SCOPE, "inv-1", PermissionStatus.DENIED)
        assert manager.authorize(SCOPE, "rm", "tc-1", "inv-1", {}) == PermissionVerdict.DENY
    finally:
        manager.shutdown(wait=False)


def test_reusing_an_invocation_id_for_another_call_raises(store: MemoryPermissionStore) -> None:
    """同一个 id 换工具 / 换模型的 `tool_call_id` = 两件不同的事，不能共用一条审批。"""
    sink = RecordingSink()
    manager = build(store, sink, waiting_policy())
    try:
        manager.authorize(SCOPE, "rm", "tc-1", "inv-1", {})
        with pytest.raises(PermissionError):
            manager.authorize(SCOPE, "rm", "tc-2", "inv-1", {})
        with pytest.raises(PermissionError):
            manager.authorize(SCOPE, "rm_rf", "tc-1", "inv-1", {})
    finally:
        manager.shutdown(wait=False)


def test_an_approval_does_not_extend_to_other_arguments(
    store: MemoryPermissionStore,
) -> None:
    """人批的是某一个具体调用，连同它的参数。换一组参数再拿同一个 id 回来，
    放行的就是一次没人看过的事 —— 转三十五块批过了，拿着同一个 id 转三万五，
    没人问过。"""
    sink = RecordingSink()
    manager = build(store, sink, waiting_policy())
    try:
        manager.authorize(SCOPE, "transfer", "tc-1", "inv-1", {"amount": 35})
        manager.decide(SCOPE, "inv-1", PermissionStatus.APPROVED)

        with pytest.raises(PermissionError):
            manager.authorize(SCOPE, "transfer", "tc-1", "inv-1", {"amount": 35000})
    finally:
        manager.shutdown(wait=False)


def test_the_same_arguments_in_another_key_order_still_replay(
    store: MemoryPermissionStore,
) -> None:
    """比的是规范化的 JSON，不是逐字段相等 —— 键序不同是同一组参数。"""
    sink = RecordingSink()
    manager = build(store, sink, waiting_policy())
    try:
        manager.authorize(SCOPE, "transfer", "tc-1", "inv-1", {"to": "b", "amount": 35})
        assert (
            manager.authorize(SCOPE, "transfer", "tc-1", "inv-1", {"amount": 35, "to": "b"})
            == PermissionVerdict.ASK
        )
    finally:
        manager.shutdown(wait=False)


def test_the_record_keeps_the_arguments_asked_about(
    store: MemoryPermissionStore,
) -> None:
    """判权限和工具真正跑起来之间隔着排队与执行。调用方手上那个 dict 还活着，
    它要是被改了，这条记录描述的就不是被问的那一次了。"""
    sink = RecordingSink()
    manager = build(store, sink, waiting_policy())
    try:
        arguments: JsonObject = {"path": "/tmp/a"}
        manager.authorize(SCOPE, "rm", "tc-1", "inv-1", arguments)
        arguments["path"] = "/tmp/b"

        assert store.get(SCOPE, "inv-1").arguments == {"path": "/tmp/a"}  # type: ignore[union-attr]
    finally:
        manager.shutdown(wait=False)


# --------------------------------------------------------------- 裁定


def test_deciding_emits_resolved(store: MemoryPermissionStore) -> None:
    sink = RecordingSink()
    manager = build(store, sink, waiting_policy())
    try:
        manager.authorize(SCOPE, "rm", "tc-1", "inv-1", {})
        assert manager.decide(SCOPE, "inv-1", PermissionStatus.DENIED, reason="越权") is True

        resolved = sink.wait_for_permission(PermissionEventType.RESOLVED)
        assert len(resolved) == 1
        assert resolved[0].status == PermissionStatus.DENIED
        assert resolved[0].payload["reason"] == "越权"
        assert manager.pending(SCOPE) == ()
    finally:
        manager.shutdown(wait=False)


def test_a_second_verdict_does_nothing(store: MemoryPermissionStore) -> None:
    """两个审批人同时点「同意」：第二个人的点击必须无效，否则工具会被跑两次。"""
    sink = RecordingSink()
    manager = build(store, sink, waiting_policy())
    try:
        manager.authorize(SCOPE, "rm", "tc-1", "inv-1", {})
        assert manager.decide(SCOPE, "inv-1", PermissionStatus.APPROVED) is True
        assert manager.decide(SCOPE, "inv-1", PermissionStatus.APPROVED) is False

        record = manager.get(SCOPE, "inv-1")
        assert record is not None
        assert record.status == PermissionStatus.APPROVED
        assert len(sink.permission_of_type(PermissionEventType.RESOLVED)) == 1
    finally:
        manager.shutdown(wait=False)


def test_deciding_an_unknown_request_reports_failure(manager: PermissionManager) -> None:
    assert manager.decide(SCOPE, "never-asked", PermissionStatus.APPROVED) is False


def test_deciding_with_a_pending_status_raises(manager: PermissionManager) -> None:
    """`decide` 只接受终态。传一个 PENDING 进来是调用方的 bug，不是「还没定」。"""
    with pytest.raises(PermissionError):
        manager.decide(SCOPE, "inv-1", PermissionStatus.PENDING)


def test_a_verdict_in_another_scope_does_not_apply(store: MemoryPermissionStore) -> None:
    """用别的 session 去批，等于没批 —— 而且不泄露它存不存在。"""
    sink = RecordingSink()
    manager = build(store, sink, waiting_policy())
    try:
        manager.authorize(SCOPE, "rm", "tc-1", "inv-1", {})
        other = Scope(user_id="u1", session_id="s2")
        assert manager.decide(other, "inv-1", PermissionStatus.APPROVED) is False

        record = manager.get(SCOPE, "inv-1")
        assert record is not None
        assert record.status == PermissionStatus.PENDING
        assert sink.permission_of_type(PermissionEventType.RESOLVED) == []
    finally:
        manager.shutdown(wait=False)


# --------------------------------------------------------------- 过期


def test_a_request_expires_without_being_touched(store: MemoryPermissionStore) -> None:
    """到点由 watchdog 判过期，不靠下次有人来查。

    一个没人再碰的请求不能永远挂着 —— 挂着就等于「随时可能被批准」。
    """
    sink = RecordingSink()
    manager = build(store, sink, waiting_policy(), ttl_seconds=0.05)
    try:
        manager.authorize(SCOPE, "rm", "tc-1", "inv-1", {})

        resolved = sink.wait_for_permission(PermissionEventType.RESOLVED)
        assert [event.status for event in resolved] == [PermissionStatus.EXPIRED]

        record = manager.get(SCOPE, "inv-1")
        assert record is not None
        assert record.status == PermissionStatus.EXPIRED
        assert manager.pending(SCOPE) == ()
    finally:
        manager.shutdown(wait=False)


def test_expiry_never_overwrites_a_verdict(store: MemoryPermissionStore) -> None:
    """人批得比 watchdog 晚一步也不能被改写 —— 终态不可被改写。"""
    sink = RecordingSink()
    manager = build(store, sink, waiting_policy(), ttl_seconds=0.05)
    try:
        manager.authorize(SCOPE, "rm", "tc-1", "inv-1", {})
        manager.decide(SCOPE, "inv-1", PermissionStatus.APPROVED)

        # 让 watchdog 醒来跑完它那一轮。它看到的已经定了，什么都不做。
        sink.wait_for_permission(PermissionEventType.RESOLVED)
        threading.Event().wait(0.3)

        record = manager.get(SCOPE, "inv-1")
        assert record is not None
        assert record.status == PermissionStatus.APPROVED
        assert [e.status for e in sink.permission_of_type(PermissionEventType.RESOLVED)] == [
            PermissionStatus.APPROVED
        ]
    finally:
        manager.shutdown(wait=False)


def test_a_verdict_returned_synchronously_from_emit_sticks(store: MemoryPermissionStore) -> None:
    """堆项先入堆再发事件。

    投递方完全可能同步回一个裁定回来；那时 watchdog 醒来必须已经看到这条请求
    不再需要管，否则它会拿一条过期裁定盖掉人的同意。
    """

    class DecidingSink(RecordingSink):
        def emit(self, event: AgentEvent) -> None:
            super().emit(event)
            if event.type == PermissionEventType.REQUESTED:
                closing.decide(SCOPE, "inv-1", PermissionStatus.APPROVED)

    sink = DecidingSink()
    closing = build(store, sink, waiting_policy(), ttl_seconds=0.05)
    try:
        closing.authorize(SCOPE, "rm", "tc-1", "inv-1", {})
        threading.Event().wait(0.3)

        record = closing.get(SCOPE, "inv-1")
        assert record is not None
        assert record.status == PermissionStatus.APPROVED
        assert [e.status for e in sink.permission_of_type(PermissionEventType.RESOLVED)] == [
            PermissionStatus.APPROVED
        ]
    finally:
        closing.shutdown(wait=False)


def test_shutdown_stops_the_watchdog(store: MemoryPermissionStore) -> None:
    """关闭之后不再有权限判定，也不该有后台线程还在跑。"""
    sink = RecordingSink()
    manager = build(store, sink, waiting_policy(), ttl_seconds=0.05)
    manager.authorize(SCOPE, "rm", "tc-1", "inv-1", {})
    manager.shutdown(wait=True)

    with pytest.raises(PermissionDeniedError):
        manager.authorize(SCOPE, "echo", "tc-2", "inv-2", {})
    assert sink.permission_of_type(PermissionEventType.RESOLVED) == []
