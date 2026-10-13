"""工具系统：派发、并发、超时、状态、事件。

并发相关的断言全部用 `threading.Event` / `Barrier`，不用 sleep —— sleep 断言的是
「等了这么久应该跑完了」，`Event` / `Barrier` 断言的是「它们确实同时在跑」。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from core.journal.validation import validate_idempotency_key, validate_payload
from core.protocol.datatype import (
    MAX_INVOCATION_ID_LENGTH,
    EventType,
    Scope,
    ToolCallRequest,
    ToolErrorKind,
    ToolEventType,
    ToolHandleState,
    ToolOutcome,
    ToolStatus,
)
from core.protocol.datatype.permission import (
    PermissionEventType,
    PermissionStatus,
    PermissionVerdict,
)
from core.protocol.error import ToolExecutionError, ToolRequestError
from core.protocol.interface import PermissionPolicy
from core.tool import ToolRegistry
from core.tool.impl import AWAITING_APPROVAL_CONTENT, ThreadedToolSystem
from core.utils.encoding import JsonObject, JsonValue
from tests._support.tool_fixtures import (
    BrokenPolicy,
    EchoTool,
    GatedTool,
    RaisingTool,
    RecordingSink,
    VerdictPolicy,
    deny,
    permission_manager,
    wait_entered,
    wait_terminal,
)

SCOPE = Scope(user_id="u1", session_id="s1")

#: 要求 `n` 是整数的 schema。用来验证入参校验真的在跑。
INT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {"n": {"type": "integer"}},
    "required": ["n"],
    "additionalProperties": False,
}


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


@pytest.fixture
def system(registry: ToolRegistry, sink: RecordingSink) -> Iterator[ThreadedToolSystem]:
    manager = permission_manager()
    ts = ThreadedToolSystem(registry, sink, manager)
    yield ts
    ts.shutdown(wait=False)
    manager.shutdown(wait=False)


def build(
    registry: ToolRegistry,
    sink: RecordingSink,
    policy: PermissionPolicy | None = None,
    **kwargs: int,
) -> ThreadedToolSystem:
    manager = permission_manager(policy)
    return ThreadedToolSystem(registry, sink, manager, **kwargs)


# --------------------------------------------------------------- 立即返回


def test_execute_returns_before_any_tool_finishes(
    system: ThreadedToolSystem, registry: ToolRegistry
) -> None:
    """派发接口唯一的保证：返回时没有一个工具跑完。"""
    gate = threading.Event()
    registry.register(GatedTool("gated", gate=gate), timeout_seconds=30.0)

    batch = system.execute(SCOPE, [ToolCallRequest(tool="gated", arguments={}, tool_call_id="tc")])

    assert batch.handles[0].state == ToolHandleState.ACCEPTED
    execution = system.status(SCOPE, batch.handles[0].invocation_id)
    assert execution is not None
    assert not execution.is_terminal

    gate.set()
    settled = wait_terminal(system, SCOPE, batch.handles[0].invocation_id)
    assert settled.status == ToolStatus.SUCCEEDED


def test_one_item_in_a_batch_never_holds_up_the_others(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    """被拒的一项不影响同批其他项 —— 一批里每一项独立。"""
    ts = build(registry, sink)
    registry.register(EchoTool("echo"), timeout_seconds=10.0)
    try:
        batch = ts.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id="tc-m1", tool="missing", arguments={}, invocation_id="m1"
                ),
                ToolCallRequest(
                    tool_call_id="tc-e1", tool="echo", arguments={"a": 1}, invocation_id="e1"
                ),
            ],
        )
        assert [h.state for h in batch.handles] == [
            ToolHandleState.REJECTED,
            ToolHandleState.ACCEPTED,
        ]
        rejected = batch.handles[0].error
        assert rejected is not None
        assert rejected.kind == ToolErrorKind.UNKNOWN_TOOL
        assert wait_terminal(ts, SCOPE, "e1").status == ToolStatus.SUCCEEDED
    finally:
        ts.shutdown(wait=False)


# --------------------------------------------------------------- 并行


def test_a_batch_runs_in_parallel(system: ThreadedToolSystem, registry: ToolRegistry) -> None:
    """一批里的调用互相独立，必须同时在跑。到不齐栅栏就直接 BrokenBarrierError。"""
    barrier = threading.Barrier(4, timeout=10)
    tool = GatedTool("gated", gate=threading.Event(), barrier=barrier)
    registry.register(tool, timeout_seconds=30.0)

    batch = system.execute(
        SCOPE,
        [
            ToolCallRequest(tool="gated", arguments={"i": i}, tool_call_id=f"tc-{i}")
            for i in range(4)
        ],
    )

    assert all(h.state == ToolHandleState.ACCEPTED for h in batch.handles)
    tool.gate.set()
    for handle in batch.handles:
        settled = wait_terminal(system, SCOPE, handle.invocation_id)
        assert settled.status == ToolStatus.SUCCEEDED
    assert tool.entered == 4


# --------------------------------------------------------------- 超时


def test_timeout_settles_and_emits(registry: ToolRegistry, sink: RecordingSink) -> None:
    ts = build(registry, sink)
    gate = threading.Event()
    registry.register(GatedTool("gated", gate=gate), timeout_seconds=0.1)
    try:
        handle = ts.execute(
            SCOPE, [ToolCallRequest(tool="gated", arguments={}, tool_call_id="tc")]
        ).handles[0]
        events = sink.wait_for_tool(1)

        assert events[0].type == ToolEventType.TERMINAL
        assert events[0].outcome == ToolStatus.TIMEOUT
        execution = ts.status(SCOPE, handle.invocation_id)
        assert execution is not None
        assert execution.status == ToolStatus.TIMEOUT
        assert execution.error is not None
        assert execution.error.kind == ToolErrorKind.TIMEOUT
    finally:
        gate.set()
        ts.shutdown(wait=False)


def test_a_late_result_never_overwrites_the_terminal_state(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    """超时后结果姗姗来迟，不改写终态 —— 终态一旦落下就是终态。"""
    ts = build(registry, sink)
    gate = threading.Event()
    tool = GatedTool("gated", gate=gate)
    registry.register(tool, timeout_seconds=0.1)
    try:
        handle = ts.execute(
            SCOPE, [ToolCallRequest(tool="gated", arguments={}, tool_call_id="tc")]
        ).handles[0]
        sink.wait_for_tool(1)
        gate.set()
        ts.shutdown(wait=True)  # 等工具线程真的跑完
        assert tool.finished == [handle.invocation_id]

        execution = ts.status(SCOPE, handle.invocation_id)
        assert execution is not None
        assert execution.status == ToolStatus.TIMEOUT
        assert len(sink.events) == 1
    finally:
        gate.set()
        ts.shutdown(wait=False)


def test_timeout_means_core_stopped_waiting_not_the_thread(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    """契约写明：超时不是终止线程。工具线程照样跑完，只是结果被丢弃。"""
    ts = build(registry, sink)
    gate = threading.Event()
    tool = GatedTool("gated", gate=gate)
    registry.register(tool, timeout_seconds=0.1)
    try:
        handle = ts.execute(
            SCOPE, [ToolCallRequest(tool="gated", arguments={}, tool_call_id="tc")]
        ).handles[0]
        sink.wait_for_tool(1)
        gate.set()
        ts.shutdown(wait=True)

        assert tool.finished == [handle.invocation_id]
        execution = ts.status(SCOPE, handle.invocation_id)
        assert execution is not None
        assert execution.status == ToolStatus.TIMEOUT
    finally:
        gate.set()
        ts.shutdown(wait=False)


def test_a_queued_call_that_times_out_never_runs(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    """排队中到点的项，worker 取到它时发现已有终态，直接跳过。"""
    ts = build(registry, sink, max_concurrency=1, max_pending=4)
    gate = threading.Event()
    blocker = GatedTool("blocker", gate=gate)
    queued = GatedTool("queued", gate=threading.Event())
    registry.register(blocker, timeout_seconds=30.0)
    registry.register(queued, timeout_seconds=0.15)
    try:
        ts.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id="tc-first", tool="blocker", arguments={}, invocation_id="first"
                )
            ],
        )
        wait_entered(blocker, 1)
        ts.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id="tc-second", tool="queued", arguments={}, invocation_id="second"
                )
            ],
        )

        assert sink.wait_for_tool(1)[0].invocation_id == "second"
        gate.set()
        ts.shutdown(wait=True)  # 等线程池里那个排队任务被取走
        assert queued.entered == 0
    finally:
        gate.set()
        ts.shutdown(wait=False)


# --------------------------------------------------------------- 权限


def test_permission_is_checked_before_every_execution(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    policy = deny({"echo"})
    ts = build(registry, sink, policy)
    registry.register(EchoTool("echo"), timeout_seconds=10.0)
    try:
        handle = ts.execute(
            SCOPE, [ToolCallRequest(tool="echo", arguments={}, tool_call_id="tc")]
        ).handles[0]
        assert handle.state == ToolHandleState.REJECTED
        assert handle.error is not None
        assert handle.error.kind == ToolErrorKind.PERMISSION_DENIED
        assert policy.calls == [("u1", "echo")]
        assert error_kind(sink.wait_for_tool(1)[0].payload) == ToolErrorKind.PERMISSION_DENIED
    finally:
        ts.shutdown(wait=False)


def test_permission_is_checked_before_the_arguments(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    """先问权限再问参数：拿不合法参数去问策略，等于把参数内容泄给不该看它的实现。

    所以「既被拒又参数不合法」时报的是权限被拒。
    """
    ts = build(registry, sink, deny({"echo"}))
    registry.register(EchoTool("echo", schema=INT_SCHEMA), timeout_seconds=10.0)
    try:
        handle = ts.execute(
            SCOPE, [ToolCallRequest(tool="echo", arguments={"n": "not-an-int"}, tool_call_id="tc")]
        ).handles[0]
        assert handle.error is not None
        assert handle.error.kind == ToolErrorKind.PERMISSION_DENIED
    finally:
        ts.shutdown(wait=False)


def test_a_denied_call_never_reaches_a_thread(registry: ToolRegistry, sink: RecordingSink) -> None:
    ts = build(registry, sink, deny({"gated"}))
    tool = GatedTool("gated", gate=threading.Event())
    registry.register(tool, timeout_seconds=10.0)
    try:
        ts.execute(SCOPE, [ToolCallRequest(tool="gated", arguments={}, tool_call_id="tc")])
        assert tool.entered == 0
    finally:
        ts.shutdown(wait=False)


def test_a_broken_policy_denies_instead_of_allowing(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    """策略自己抛错 = 没得出结论。core fail closed：判成拒绝，不放行。"""
    ts = build(registry, sink, BrokenPolicy())
    tool = EchoTool("echo")
    registry.register(tool, timeout_seconds=10.0)
    try:
        handle = ts.execute(
            SCOPE, [ToolCallRequest(tool="echo", arguments={}, tool_call_id="tc")]
        ).handles[0]
        assert handle.state == ToolHandleState.REJECTED
        assert handle.error is not None
        assert handle.error.kind == ToolErrorKind.PERMISSION_DENIED
    finally:
        ts.shutdown(wait=False)


def test_a_permission_failure_rejects_one_item_without_dropping_the_batch(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    """权限这一步没能完成时，折成**这一项**被拒，不抛出去。

    抛出去的最差结果是：同批其他项已经接纳、已经在跑，调用方却拿不到它们的 id。
    """
    ts = build(registry, sink, VerdictPolicy({"broken": "不是三值之一"}))
    registry.register(EchoTool("broken"), timeout_seconds=10.0)
    registry.register(EchoTool("fine"), timeout_seconds=10.0)
    try:
        batch = ts.execute(
            SCOPE,
            [
                ToolCallRequest(tool="broken", arguments={}, tool_call_id="tc-1"),
                ToolCallRequest(tool="fine", arguments={}, tool_call_id="tc-2"),
            ],
        )
        assert [h.state for h in batch.handles] == [
            ToolHandleState.REJECTED,
            ToolHandleState.ACCEPTED,
        ]
        failed = batch.handles[0].error
        assert failed is not None
        assert failed.kind == ToolErrorKind.PERMISSION_ERROR
        assert wait_terminal(ts, SCOPE, batch.handles[1].invocation_id).status == (
            ToolStatus.SUCCEEDED
        )
    finally:
        ts.shutdown(wait=False)


# --------------------------------------------------------------- 待审批


def test_an_ask_call_is_not_executed_but_still_gets_a_terminal_event(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    """ask 的当场结论：模型那个槽位必须被填上，但没有任何东西被执行。"""
    perm_sink = RecordingSink()
    manager = permission_manager(
        VerdictPolicy({"gated": PermissionVerdict.ASK}), sink=perm_sink, ttl_seconds=30.0
    )
    ts = ThreadedToolSystem(registry, sink, manager)
    tool = GatedTool("gated", gate=threading.Event())
    registry.register(tool, timeout_seconds=10.0)
    try:
        handle = ts.execute(
            SCOPE, [ToolCallRequest(tool="gated", arguments={}, tool_call_id="tc")]
        ).handles[0]
        assert handle.state == ToolHandleState.PENDING_APPROVAL
        assert tool.entered == 0

        event = sink.wait_for_tool(1)[0]
        assert event.outcome == ToolOutcome.AWAITING_APPROVAL
        assert event.payload["content"] == AWAITING_APPROVAL_CONTENT
        # 不进执行表：没有任何东西被执行过，「这个 id 跑成什么」暂时无从谈起。
        assert ts.status(SCOPE, handle.invocation_id) is None

        requested = perm_sink.permission_of_type(PermissionEventType.REQUESTED)
        assert len(requested) == 1
        assert requested[0].invocation_id == handle.invocation_id
        assert requested[0].status == PermissionStatus.PENDING
    finally:
        ts.shutdown(wait=False)
        manager.shutdown(wait=False)


def test_an_ask_call_does_not_take_an_execution_slot(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    """待审批的调用不占 worker 也不占名额：一个塞满待审批的系统不该因此完全瘫痪。"""
    manager = permission_manager(VerdictPolicy(default=PermissionVerdict.ASK), ttl_seconds=30.0)
    ts = ThreadedToolSystem(registry, sink, manager, max_pending=1)
    registry.register(EchoTool("echo"), timeout_seconds=10.0)
    try:
        batch = ts.execute(
            SCOPE,
            [
                ToolCallRequest(tool="echo", arguments={}, tool_call_id="tc1"),
                ToolCallRequest(tool="echo", arguments={}, tool_call_id="tc2"),
            ],
        )
        assert [h.state for h in batch.handles] == [
            ToolHandleState.PENDING_APPROVAL,
            ToolHandleState.PENDING_APPROVAL,
        ]
    finally:
        ts.shutdown(wait=False)
        manager.shutdown(wait=False)


def test_the_awaiting_approval_event_is_a_legal_tool_result(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    """这条终态事件要能直接进 journal —— 事件 payload 恰好是合法 `tool.result` payload。"""
    manager = permission_manager(VerdictPolicy(default=PermissionVerdict.ASK), ttl_seconds=30.0)
    ts = ThreadedToolSystem(registry, sink, manager)
    registry.register(EchoTool("echo"), timeout_seconds=10.0)
    try:
        ts.execute(SCOPE, [ToolCallRequest(tool="echo", arguments={}, tool_call_id="tc")])
        validate_payload(EventType.TOOL_RESULT, 1, sink.wait_for_tool(1)[0].payload)
    finally:
        ts.shutdown(wait=False)
        manager.shutdown(wait=False)


# --------------------------------------------------------------- 入参


def test_bad_arguments_are_rejected_at_admission(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    """不合 schema 的参数没有资格占线程。"""
    ts = build(registry, sink)
    tool = GatedTool("gated", gate=threading.Event(), schema=INT_SCHEMA)
    registry.register(tool, timeout_seconds=10.0)
    try:
        handle = ts.execute(
            SCOPE,
            [ToolCallRequest(tool="gated", arguments={"n": "not-an-int"}, tool_call_id="tc")],
        ).handles[0]
        assert handle.state == ToolHandleState.REJECTED
        assert handle.error is not None
        assert handle.error.kind == ToolErrorKind.INVALID_ARGUMENTS
        assert tool.entered == 0
        assert sink.wait_for_tool(1)[0].outcome == ToolStatus.FAILED
    finally:
        ts.shutdown(wait=False)


def test_valid_arguments_pass_through(registry: ToolRegistry, sink: RecordingSink) -> None:
    ts = build(registry, sink)
    registry.register(EchoTool("echo", schema=INT_SCHEMA), timeout_seconds=10.0)
    try:
        ts.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id="tc-a1", tool="echo", arguments={"n": 7}, invocation_id="a1"
                )
            ],
        )
        execution = wait_terminal(ts, SCOPE, "a1")
        assert execution.status == ToolStatus.SUCCEEDED
        assert execution.result is not None
        assert execution.result.content == {"n": 7}
    finally:
        ts.shutdown(wait=False)


# --------------------------------------------------------------- 去重


def test_same_invocation_runs_once(registry: ToolRegistry, sink: RecordingSink) -> None:
    """同一个调用重复派发不重复执行 —— 这是不重复施加外部副作用的那道闸。"""
    ts = build(registry, sink)
    gate = threading.Event()
    tool = GatedTool("gated", gate=gate)
    registry.register(tool, timeout_seconds=30.0)
    try:
        request = ToolCallRequest(
            tool_call_id="tc-dup", tool="gated", arguments={}, invocation_id="dup"
        )
        first = ts.execute(SCOPE, [request]).handles[0]
        second = ts.execute(SCOPE, [request]).handles[0]

        assert first.state == ToolHandleState.ACCEPTED
        assert second.state == ToolHandleState.DEDUPLICATED
        assert second.invocation_id == "dup"

        gate.set()
        sink.wait_for_tool(1)
        assert tool.finished == ["dup"]
        assert len(sink.events) == 1
    finally:
        gate.set()
        ts.shutdown(wait=False)


def test_same_invocation_in_another_scope_is_a_different_call(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    ts = build(registry, sink)
    registry.register(EchoTool("echo"), timeout_seconds=10.0)
    try:
        request = ToolCallRequest(
            tool_call_id="tc-shared", tool="echo", arguments={}, invocation_id="shared"
        )
        assert ts.execute(SCOPE, [request]).handles[0].state == ToolHandleState.ACCEPTED
        other = Scope(user_id="u1", session_id="s2")
        assert ts.execute(other, [request]).handles[0].state == ToolHandleState.ACCEPTED
    finally:
        ts.shutdown(wait=False)


# --------------------------------------------------------------- 名额


def test_capacity_exhaustion_rejects_instead_of_queueing_forever(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    ts = build(registry, sink, max_concurrency=1, max_pending=1)
    gate = threading.Event()
    registry.register(GatedTool("gated", gate=gate), timeout_seconds=30.0)
    try:
        first = ts.execute(
            SCOPE, [ToolCallRequest(tool="gated", arguments={}, tool_call_id="tc")]
        ).handles[0]
        second = ts.execute(
            SCOPE, [ToolCallRequest(tool="gated", arguments={}, tool_call_id="tc")]
        ).handles[0]
        assert first.state == ToolHandleState.ACCEPTED
        assert second.state == ToolHandleState.REJECTED
        assert second.error is not None
        assert second.error.kind == ToolErrorKind.BUSY
    finally:
        gate.set()
        ts.shutdown(wait=False)


# --------------------------------------------------------------- 状态查询


def test_status_is_scoped(registry: ToolRegistry, sink: RecordingSink) -> None:
    """跨 session 查不到别人的执行，也不泄露「这个 id 存在」。"""
    ts = build(registry, sink)
    registry.register(EchoTool("echo"), timeout_seconds=10.0)
    try:
        ts.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id="tc-q1", tool="echo", arguments={"a": 1}, invocation_id="q1"
                )
            ],
        )
        mine = wait_terminal(ts, SCOPE, "q1")

        assert mine.result is not None
        assert mine.result.content == {"a": 1}
        assert mine.started_dt is not None
        assert mine.finished_dt is not None
        assert mine.outcome == ToolOutcome.SUCCEEDED

        assert ts.status(Scope(user_id="u1", session_id="s2"), "q1") is None
        assert ts.status(Scope(user_id="u2", session_id="s1"), "q1") is None
        assert ts.status(SCOPE, "never-existed") is None
    finally:
        ts.shutdown(wait=False)


def test_status_reports_a_call_while_it_runs(registry: ToolRegistry, sink: RecordingSink) -> None:
    gate = threading.Event()
    ts = build(registry, sink)
    tool = GatedTool("gated", gate=gate)
    registry.register(tool, timeout_seconds=30.0)
    try:
        handle = ts.execute(
            SCOPE, [ToolCallRequest(tool="gated", arguments={}, tool_call_id="tc")]
        ).handles[0]
        wait_entered(tool, 1)
        execution = ts.status(SCOPE, handle.invocation_id)
        assert execution is not None
        assert execution.status == ToolStatus.RUNNING
        assert execution.started_dt is not None
        assert not execution.is_terminal

        gate.set()
        assert wait_terminal(ts, SCOPE, handle.invocation_id).status == ToolStatus.SUCCEEDED
    finally:
        gate.set()
        ts.shutdown(wait=False)


def test_rejected_calls_have_no_execution_record(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    """没被执行过的东西不进执行表 —— 执行表只回答「跑成了什么」。"""
    ts = build(registry, sink)
    try:
        handle = ts.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id="tc-r1", tool="missing", arguments={}, invocation_id="r1"
                )
            ],
        ).handles[0]
        assert ts.status(SCOPE, "r1") is None
        assert handle.error is not None
    finally:
        ts.shutdown(wait=False)


def test_terminal_history_is_bounded(registry: ToolRegistry, sink: RecordingSink) -> None:
    """长期运行的进程里，执行表不能只增不减。"""
    ts = build(registry, sink, max_history=8)
    registry.register(EchoTool("echo"), timeout_seconds=10.0)
    try:
        ids = [f"e{i:02d}" for i in range(30)]
        for invocation_id in ids:
            ts.execute(
                SCOPE,
                [
                    ToolCallRequest(
                        tool="echo",
                        arguments={},
                        tool_call_id=f"tc-{invocation_id}",
                        invocation_id=invocation_id,
                    )
                ],
            )
            wait_terminal(ts, SCOPE, invocation_id)

        visible = [i for i in ids if ts.status(SCOPE, i) is not None]
        assert len(visible) <= 8
        assert ids[-1] in visible
        assert ids[0] not in visible
    finally:
        ts.shutdown(wait=False)


# --------------------------------------------------------------- 事件


def test_every_call_gets_exactly_one_terminal_event(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    ts = build(registry, sink)
    registry.register(EchoTool("echo"), timeout_seconds=10.0)
    try:
        ts.execute(
            SCOPE,
            # 最后一项当场被拒：它没有执行记录，但**仍然要发终态事件**，否则 agent
            # 拿不到 tool.result，模型留下的 tool_calls 空位填不上。
            [
                ToolCallRequest(
                    tool="echo", arguments={"i": i}, tool_call_id=f"tc-k{i}", invocation_id=f"k{i}"
                )
                for i in range(5)
            ]
            + [
                ToolCallRequest(
                    tool_call_id="tc-k5", tool="missing", arguments={}, invocation_id="k5"
                )
            ],
        )
        events = sink.wait_for_tool(6)

        assert len(events) == 6
        assert all(event.type == ToolEventType.TERMINAL for event in events)
        assert {event.invocation_id for event in events} == {"k0", "k1", "k2", "k3", "k4", "k5"}
    finally:
        ts.shutdown(wait=False)


def test_event_payload_is_a_legal_tool_result_record(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    """事件 payload 逐字段对齐 journal 的 `tool.result` schema —— sink 可以直接 append。"""
    ts = build(registry, sink)
    registry.register(EchoTool("echo"), timeout_seconds=10.0)
    registry.register(RaisingTool("boom", ToolExecutionError("业务上不行")))
    try:
        ts.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id="tc-p1", tool="echo", arguments={"a": 1}, invocation_id="p1"
                )
            ],
        )
        ts.execute(
            SCOPE,
            [ToolCallRequest(tool_call_id="tc-p2", tool="boom", arguments={}, invocation_id="p2")],
        )
        ts.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id="tc-p3", tool="missing", arguments={}, invocation_id="p3"
                )
            ],
        )
        for event in sink.wait_for_tool(3):
            validate_payload(EventType.TOOL_RESULT, 1, event.payload)
            validate_idempotency_key(event.idempotency_key)
            assert event.payload["outcome"] == event.outcome
            assert event.payload["invocation_id"] == event.invocation_id
            assert event.payload["tool_call_id"] == event.tool_call_id
    finally:
        ts.shutdown(wait=False)


def test_success_and_failure_payloads_carry_the_right_fields(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    ts = build(registry, sink)
    registry.register(EchoTool("echo"), timeout_seconds=10.0)
    registry.register(RaisingTool("boom", ToolExecutionError("业务上不行")))
    registry.register(RaisingTool("crash", ZeroDivisionError("手滑")))
    try:
        ts.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id="tc-f1", tool="echo", arguments={"a": 1}, invocation_id="f1"
                )
            ],
        )
        ts.execute(
            SCOPE,
            [ToolCallRequest(tool_call_id="tc-f2", tool="boom", arguments={}, invocation_id="f2")],
        )
        ts.execute(
            SCOPE,
            [ToolCallRequest(tool_call_id="tc-f3", tool="crash", arguments={}, invocation_id="f3")],
        )
        events = {event.invocation_id: event for event in sink.wait_for_tool(3)}
        assert len(events) == 3

        assert events["f1"].outcome == ToolOutcome.SUCCEEDED
        assert events["f1"].payload["content"] == {"a": 1}
        assert "error" not in events["f1"].payload

        assert events["f2"].payload["error"] == {
            "kind": ToolErrorKind.TOOL_ERROR,
            "message": "ToolExecutionError: 业务上不行",
        }
        assert error_kind(events["f3"].payload) == ToolErrorKind.UNEXPECTED
        # traceback 是调试信息，不是事件内容。
        assert "Traceback" not in error_message(events["f3"].payload)
    finally:
        ts.shutdown(wait=False)


def test_sink_failure_leaves_the_outcome_unknown(registry: ToolRegistry) -> None:
    """结果确实存在过，只是没送出去 —— 不能记成「没跑过」，否则会被重试。"""
    failing = RecordingSink(fail=True)
    ts = build(registry, failing)
    registry.register(EchoTool("echo"), timeout_seconds=10.0)
    try:
        ts.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id="tc-u1", tool="echo", arguments={"a": 1}, invocation_id="u1"
                )
            ],
        )
        execution = wait_terminal(ts, SCOPE, "u1")

        assert execution.status == ToolStatus.OUTCOME_UNKNOWN
        assert execution.outcome == ToolOutcome.OUTCOME_UNKNOWN
        assert execution.error is not None
        assert execution.error.kind == ToolErrorKind.SINK_ERROR
        # 投递过、失败过，但不再重试 —— 送不出去时重试只会反复失败。
        assert failing.events == []
    finally:
        ts.shutdown(wait=False)


# --------------------------------------------------------------- 批次入参


def test_empty_batch_is_a_caller_bug(registry: ToolRegistry, sink: RecordingSink) -> None:
    ts = build(registry, sink)
    try:
        with pytest.raises(ToolRequestError):
            ts.execute(SCOPE, [])
    finally:
        ts.shutdown(wait=False)


def test_the_model_tool_call_id_is_echoed_verbatim(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    """模型给的 `tool_call_id` 原样回到终态事件里，一个字符都不改。

    组对话历史时 assistant 轮的 `tool_calls[i].id` 与 tool 消息的 `tool_call_id`
    靠它配对；任何 provider 对不上就拒收整条请求。所以它必须原样透传，
    既不能改写，也不能拿 Oath 自己的 `invocation_id` 顶替。
    """
    ts = build(registry, sink)
    registry.register(EchoTool("echo"), timeout_seconds=10.0)
    try:
        model_id = "toolu_01AbC-xY9"
        ts.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool="echo",
                    arguments={"a": 1},
                    tool_call_id=model_id,
                    invocation_id="i-1",
                )
            ],
        )
        event = sink.wait_for_tool(1)[0]
        assert event.tool_call_id == model_id
        assert event.payload["tool_call_id"] == model_id
        assert event.invocation_id == "i-1"
        assert event.payload["invocation_id"] == "i-1"
        # 两个 id 互不冒充。
        assert event.tool_call_id != event.invocation_id
    finally:
        ts.shutdown(wait=False)


def test_rejected_calls_still_echo_the_tool_call_id(
    registry: ToolRegistry, sink: RecordingSink
) -> None:
    """当场被拒的调用不发线程，但**照样要把模型那个槽位填掉** —— 没有回声的话
    对话里会留一条没有结果的 tool_calls，任何 provider 都收不了这个数组。"""
    ts = build(registry, sink)
    try:
        handle = ts.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool="missing", arguments={}, tool_call_id="toolu_x", invocation_id="i-2"
                )
            ],
        ).handles[0]
        assert handle.state == ToolHandleState.REJECTED
        assert sink.wait_for_tool(1)[0].tool_call_id == "toolu_x"
    finally:
        ts.shutdown(wait=False)


def test_overlong_invocation_id_is_rejected(registry: ToolRegistry, sink: RecordingSink) -> None:
    """只卡长度，不卡字符集。

    `invocation_id` 由 Oath 生成，形态是实现细节；硬约束只有一条 —— 拼上
    `tool.result:` 之后要落在 journal 的幂等键长度上限内。
    """
    ts = build(registry, sink)
    registry.register(EchoTool("echo"), timeout_seconds=10.0)
    try:
        with pytest.raises(ToolRequestError):
            ts.execute(
                SCOPE,
                [
                    ToolCallRequest(
                        tool="echo",
                        arguments={},
                        tool_call_id="tc",
                        invocation_id="x" * (MAX_INVOCATION_ID_LENGTH + 1),
                    )
                ],
            )
        # 空格不是问题 —— 它只是 Oath 生成的 id，journal 不对 payload 字段做字符集校验。
        batch = ts.execute(
            SCOPE,
            [ToolCallRequest(tool="echo", arguments={}, tool_call_id="tc", invocation_id="a b /c")],
        )
        assert batch.handles[0].invocation_id == "a b /c"
    finally:
        ts.shutdown(wait=False)


def test_invocation_id_is_generated_when_omitted(
    system: ThreadedToolSystem, registry: ToolRegistry
) -> None:
    registry.register(EchoTool("echo"), timeout_seconds=10.0)
    batch = system.execute(SCOPE, [ToolCallRequest(tool="echo", arguments={}, tool_call_id="tc")])
    assert len(batch.handles[0].invocation_id) == 32


def test_shutdown_stops_accepting_work(registry: ToolRegistry, sink: RecordingSink) -> None:
    ts = build(registry, sink)
    registry.register(EchoTool("echo"), timeout_seconds=10.0)
    ts.shutdown()
    with pytest.raises(ToolRequestError):
        ts.execute(SCOPE, [ToolCallRequest(tool="echo", arguments={}, tool_call_id="tc")])


def test_definitions_come_from_the_registry(
    system: ThreadedToolSystem, registry: ToolRegistry
) -> None:
    registry.register(EchoTool("b"))
    registry.register(EchoTool("a"))
    assert [d.name for d in system.definitions()] == ["a", "b"]


def error_kind(payload: JsonObject) -> JsonValue:
    return _error(payload)["kind"]


def error_message(payload: JsonObject) -> str:
    value = _error(payload)["message"]
    assert isinstance(value, str)
    return value


def _error(payload: JsonObject) -> JsonObject:
    error = payload["error"]
    assert isinstance(error, dict)
    return error
