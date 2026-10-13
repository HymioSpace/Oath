"""`JournalIntake` —— command 怎么变成队列里的一条记录，以及消费时去哪。"""

from __future__ import annotations

import pytest

from core.intake import JournalIntake, TypeRouter
from core.protocol.datatype import Cursor, CursorKind, EventType, Record, Scope
from core.protocol.datatype.queue import INPUT_QUEUE, QueueName
from core.protocol.datatype.wire import Command, CommandType
from core.protocol.error import WireProtocolError
from core.protocol.interface import EventQueue, Journal
from core.protocol.interface.intake import RoutingDecision, SessionAuthenticator, SessionIdentity
from core.utils.encoding import JsonValue, utc_now_iso

# --------------------------------------------------------------------------
# 替身
# --------------------------------------------------------------------------


class _FakeQueue(EventQueue):
    """三条 `list`，按 `INPUT_QUEUE` 的映射进出。记下取出顺序。"""

    def __init__(self) -> None:
        self.lines: dict[str, list[Record]] = {name: [] for name in QueueName.ALL}
        self.taken: list[tuple[str, Record]] = []

    def put(self, scope: Scope, record: Record) -> None:
        self.lines[INPUT_QUEUE[record.type]].append(record)

    def get(self, scope: Scope, queue: str, *, timeout: float | None = None) -> Record | None:
        if not self.lines[queue]:
            return None
        record = self.lines[queue].pop(0)
        self.taken.append((queue, record))
        return record

    def pending(self, scope: Scope, queue: str) -> int:
        return len(self.lines[queue])

    def wait(self, scope: Scope, *, timeout: float | None = None) -> bool:
        return any(self.lines.values())

    def close(self, *, wait: bool = True) -> None:
        return None

    def queue_of(self, type_: str) -> str:
        return INPUT_QUEUE[type_]


class _Brain:
    """记下 brain 收到过哪些记录。真实签名不在这层，测试只看路由结果。"""

    def __init__(self) -> None:
        self.steps: list[Record] = []

    def step(self, scope: Scope, record: Record) -> None:
        self.steps.append(record)

    def close(self, *, wait: bool = True) -> None:
        return None


class _Auth(SessionAuthenticator):
    def __init__(self, identity: SessionIdentity) -> None:
        self._identity = identity
        self.seen: list[str] = []

    def authenticate(self, credential: str) -> SessionIdentity:
        self.seen.append(credential)
        return self._identity


def _command(type_: str, cid: str, session_id: str | None = "s1", **payload: JsonValue) -> Command:
    return Command(id=cid, type=type_, session_id=session_id, payload=payload)


def _intake(journal: Journal) -> tuple[JournalIntake, _FakeQueue, _Brain]:
    queue = _FakeQueue()
    brain = _Brain()
    return JournalIntake(journal, queue, brain, TypeRouter()), queue, brain  # type: ignore[arg-type]


def _types(journal: Journal, scope: Scope) -> list[str]:
    return [r.type for r in journal.read_range(scope, Cursor(CursorKind.LINE, 0))]


def _stub(scope: Scope, type_: str, key: str) -> Record:
    """一条不入队的记录。占位用 —— 位置不重要，只关心 type 与 payload。"""
    return Record.of(
        Cursor(CursorKind.LINE, 0),
        {
            "user_id": scope.user_id,
            "session_id": scope.session_id,
            "type": type_,
            "payload": {},
            "payload_schema_version": 1,
            "idempotency_key": key,
            "occur_dt": None,
            "commit_dt": utc_now_iso(),
        },
    )


# --------------------------------------------------------------------------
# 入站
# --------------------------------------------------------------------------


def test_a_user_message_is_journalled(journal: Journal, scope: Scope) -> None:
    intake, _, _ = _intake(journal)

    intake.handle(_command(CommandType.USER_MESSAGE, "c1", content="你好"), scope)

    assert _types(journal, scope) == [EventType.USER_MESSAGE]


def test_the_content_reaches_the_record(journal: Journal, scope: Scope) -> None:
    intake, _, _ = _intake(journal)

    intake.handle(_command(CommandType.USER_MESSAGE, "c1", content="你好"), scope)

    record = next(iter(journal.read_range(scope, Cursor(CursorKind.LINE, 0))))
    assert record.payload["content"] == "你好"


def test_the_command_id_becomes_the_idempotency_key(journal: Journal, scope: Scope) -> None:
    """重发同一个 command 是重放 —— 幂等键必须挂在 command id 上。"""
    intake, queue, _ = _intake(journal)
    command = _command(CommandType.USER_MESSAGE, "c1", content="你好")

    intake.handle(command, scope)
    intake.handle(command, scope)

    assert _types(journal, scope) == [EventType.USER_MESSAGE]
    assert queue.pending(scope, QueueName.USER) == 2


def test_two_different_commands_are_two_records(journal: Journal, scope: Scope) -> None:
    intake, _, _ = _intake(journal)

    intake.handle(_command(CommandType.USER_MESSAGE, "c1", content="一"), scope)
    intake.handle(_command(CommandType.USER_MESSAGE, "c2", content="二"), scope)

    assert _types(journal, scope) == [EventType.USER_MESSAGE, EventType.USER_MESSAGE]


def test_the_queued_record_is_the_one_that_was_written(journal: Journal, scope: Scope) -> None:
    """队列里放的就是刚 append 的那一条 —— 不是另一份内容相近的副本。"""
    intake, queue, _ = _intake(journal)

    intake.handle(_command(CommandType.USER_MESSAGE, "c1", content="你好"), scope)

    queued = queue.lines[QueueName.USER][0]
    written = next(iter(journal.read_range(scope, Cursor(CursorKind.LINE, 0))))
    assert queued.payload == written.payload
    assert queued.type == written.type


def test_ping_writes_nothing(journal: Journal, scope: Scope) -> None:
    intake, queue, _ = _intake(journal)

    intake.handle(_command(CommandType.PING, "c1"), scope)

    assert _types(journal, scope) == []
    assert queue.pending(scope, QueueName.USER) == 0


def test_run_cancel_is_journalled(journal: Journal, scope: Scope) -> None:
    intake, _, _ = _intake(journal)

    intake.handle(_command(CommandType.RUN_CANCEL, "c1", reason="用户按了停"), scope)

    record = next(iter(journal.read_range(scope, Cursor(CursorKind.LINE, 0))))
    assert record.type == EventType.SYSTEM_CANCEL
    assert record.payload["reason"] == "用户按了停"


def test_run_cancel_works_without_a_reason(journal: Journal, scope: Scope) -> None:
    intake, _, _ = _intake(journal)

    intake.handle(_command(CommandType.RUN_CANCEL, "c1"), scope)

    assert _types(journal, scope) == [EventType.SYSTEM_CANCEL]


def test_permission_decide_is_refused(journal: Journal, scope: Scope) -> None:
    """还没接进 journal。静默吞掉的话，客户端点了「同意」什么也不会发生。"""
    intake, _, _ = _intake(journal)

    with pytest.raises(WireProtocolError):
        intake.handle(_command(CommandType.PERMISSION_DECIDE, "c1", status="approved"), scope)

    assert _types(journal, scope) == []


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------


def test_open_session_takes_the_identity_from_the_credential(journal: Journal) -> None:
    intake, _, _ = _intake(journal)
    auth = _Auth(SessionIdentity(user_id="u9", session_id="s9"))

    bound = intake.open_session(
        _command(CommandType.SESSION_OPEN, "c1", session_id=None, credential="tok"),
        authenticator=auth,
    )

    assert bound == Scope(user_id="u9", session_id="s9")
    assert auth.seen == ["tok"]


def test_open_session_does_not_run_the_brain(journal: Journal, scope: Scope) -> None:
    """绑会话不是一条输入。"""
    intake, queue, brain = _intake(journal)
    auth = _Auth(SessionIdentity(user_id="u9", session_id="s9"))

    intake.open_session(
        _command(CommandType.SESSION_OPEN, "c1", session_id=None, credential="tok"),
        authenticator=auth,
    )

    assert brain.steps == []
    assert queue.pending(scope, QueueName.USER) == 0


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------


def test_a_user_message_goes_to_the_brain(journal: Journal, scope: Scope) -> None:
    intake, _, brain = _intake(journal)
    intake.handle(_command(CommandType.USER_MESSAGE, "c1", content="你好"), scope)

    assert intake.poll(scope) is True
    assert [r.type for r in brain.steps] == [EventType.USER_MESSAGE]


def test_a_tool_result_goes_to_the_brain(journal: Journal, scope: Scope) -> None:
    """工具跑完了也是一次模型调用的触发条件。"""
    intake, queue, brain = _intake(journal)
    record = Record.of(
        journal.append(
            scope,
            EventType.TOOL_RESULT,
            {
                "tool_call_id": "t-1",
                "invocation_id": "i-1",
                "outcome": "succeeded",
                "content": "ok",
            },
            payload_schema_version=1,
            idempotency_key="r1",
        ).cursor,
        {
            "user_id": scope.user_id,
            "session_id": scope.session_id,
            "type": EventType.TOOL_RESULT,
            "payload": {},
            "payload_schema_version": 1,
            "idempotency_key": "r1",
            "occur_dt": None,
            "commit_dt": "2026-10-13T00:00:00Z",
        },
    )
    queue.put(scope, record)

    assert intake.poll(scope) is True
    assert brain.steps == [record]


def test_run_cancel_does_not_reach_the_brain(journal: Journal, scope: Scope) -> None:
    """控制面指令是 core 自己处理掉的 —— 送进 brain 等于让模型自己决定要不要停。"""
    intake, _, brain = _intake(journal)
    intake.handle(_command(CommandType.RUN_CANCEL, "c1"), scope)

    assert intake.poll(scope) is True
    assert brain.steps == []


def test_run_cancel_is_recorded_as_consumed(journal: Journal, scope: Scope) -> None:
    """trace 里要看得见这次指令被消费过，而不是被队列吞掉。"""
    intake, _, _ = _intake(journal)
    intake.handle(_command(CommandType.RUN_CANCEL, "c1"), scope)

    intake.poll(scope)

    assert _types(journal, scope) == [EventType.SYSTEM_CANCEL, EventType.SYSTEM_AGENT_STEP]


def test_polling_an_empty_queue_does_nothing(journal: Journal, scope: Scope) -> None:
    intake, _, brain = _intake(journal)

    assert intake.poll(scope) is False
    assert brain.steps == []


def test_poll_takes_one_record_per_queue(journal: Journal, scope: Scope) -> None:
    """一次清空三条队列会让 system 指令被一串模型调用饿死。"""
    intake, queue, brain = _intake(journal)
    for index in range(3):
        queue.put(scope, _stub(scope, EventType.USER_MESSAGE, f"u{index}"))
    intake.handle(_command(CommandType.RUN_CANCEL, "c1"), scope)

    assert intake.poll(scope) is True
    assert len(brain.steps) == 1
    assert queue.pending(scope, QueueName.USER) == 2


def test_the_user_queue_is_served_before_the_system_queue(journal: Journal, scope: Scope) -> None:
    intake, queue, _ = _intake(journal)
    intake.handle(_command(CommandType.USER_MESSAGE, "c1", content="你好"), scope)
    intake.handle(_command(CommandType.RUN_CANCEL, "c2"), scope)

    intake.poll(scope)

    assert [queue_of for queue_of, _ in queue.taken] == [QueueName.USER, QueueName.SYSTEM]


# --------------------------------------------------------------------------
# 路由规则本身
# --------------------------------------------------------------------------


def test_the_router_sends_inputs_to_the_brain() -> None:
    router = TypeRouter()
    assert (
        router.route(_stub(Scope("u", "s"), EventType.USER_MESSAGE, "k"))
        == RoutingDecision.TO_BRAIN
    )
    assert (
        router.route(_stub(Scope("u", "s"), EventType.TOOL_RESULT, "k")) == RoutingDecision.TO_BRAIN
    )


def test_the_router_handles_the_rest_itself() -> None:
    router = TypeRouter()
    assert (
        router.route(_stub(Scope("u", "s"), EventType.SYSTEM_CANCEL, "k"))
        == RoutingDecision.BY_RULE
    )


def test_the_router_has_an_answer_for_every_type() -> None:
    """每个 type 都要有去向 —— 没有默认分支掉进未定义行为。"""
    router = TypeRouter()
    for type_ in EventType.ALL:
        assert router.route(_stub(Scope("u", "s"), type_, "k")) in RoutingDecision.ALL
