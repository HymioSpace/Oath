"""biz 的 HTTP 面：路由、身份、参数、状态码。

读模型本身的语义（span 数、耗时、空态、分页）在 `test_biz_trace.py` 里，这里只看
传输层 —— 请求进来之后拿到什么、拿不到时回什么。用真实装配与文件后端：这一层
要验的正是「从 HTTP 打进去能不能原样取回来」。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from biz.application import BizApplication
from bootstrap.biz.assembly import build_application
from bootstrap.biz.server import build_app
from bootstrap.manager.manager import SessionEntry, SessionManager
from core.protocol.datatype.config import OathConfig
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.trace import Span, SpanStatus, new_trace_id
from core.protocol.interface.agent_access import AgentAccess
from core.protocol.interface.trace import TraceStore
from core.trace.impl import FileTraceStore
from core.utils.encoding import JsonObject
from tests.test_service_boundaries import OfflineManager

TOKEN = "synthetic-service-token"
LIST = "/list_traces"
QUERY = "/query_trace"

SPAN = Span(
    trace_id=new_trace_id(),
    span_id="0000000000000001",
    name="invoke_agent",
    start_unix_nano=1_000_000,
    end_unix_nano=3_000_000,
    status=SpanStatus.OK,
)


class Unreachable(AgentAccess):
    """这个文件只验传输层，不需要真的 agent。"""

    def user_context(self, scope: Scope) -> JsonObject:
        raise AssertionError("不该走到 agent")


@pytest.fixture
def config(tmp_path: Path) -> OathConfig:
    return OathConfig(data_root=tmp_path, service_credentials=(TOKEN,))


@pytest.fixture
def manager(config: OathConfig) -> Iterator[OfflineManager]:
    """登记 session 但不拉进程 —— 这里验的是路由，不是进程模型。"""
    instance = OfflineManager(config, "")
    yield instance
    instance.close()


@pytest.fixture
def session(manager: OfflineManager) -> tuple[Scope, str]:
    """session 级凭证只认这一个 scope —— trace 路由拿它换出 scope。"""
    issued = manager.create_session("u1")
    return Scope("u1", str(issued["session_id"])), str(issued["credential"])


@pytest.fixture
def scope(session: tuple[Scope, str]) -> Scope:
    return session[0]


@pytest.fixture
def auth(session: tuple[Scope, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {session[1]}"}


@pytest.fixture
def traces(config: OathConfig) -> FileTraceStore:
    return FileTraceStore(config.data_root / "traces")


@pytest.fixture
def client(config: OathConfig, manager: OfflineManager) -> TestClient:
    return TestClient(build_app(build_application(config, manager), manager, config))


def _biz(manager: OfflineManager, traces: TraceStore) -> BizApplication:
    """biz 自己持有 trace store —— 替身要装在 biz 上。"""
    return BizApplication(Unreachable(), manager, (), traces)


class _SlowStore(TraceStore):
    """读一次要睡 100ms。用来验查询有没有把事件循环占住。"""

    def append(self, scope: Scope, span: Span) -> None:
        raise NotImplementedError

    def trace_ids(self, scope: Scope) -> tuple[str, ...]:
        time.sleep(0.1)
        return ()

    def spans(self, scope: Scope, trace_id: str) -> tuple[Span, ...]:
        time.sleep(0.1)
        return ()


class _ExplodingStore(TraceStore):
    """一读就炸。用来验 HTTP 层把读失败报成 500 而不是空列表。"""

    def append(self, scope: Scope, span: Span) -> None:
        raise OSError("写不进去（测试）")

    def trace_ids(self, scope: Scope) -> tuple[str, ...]:
        raise OSError("读不出来（测试）")

    def spans(self, scope: Scope, trace_id: str) -> tuple[Span, ...]:
        raise OSError("读不出来（测试）")


def test_a_credential_carries_the_scope(
    client: TestClient, traces: FileTraceStore, scope: Scope, auth: dict[str, str]
) -> None:
    traces.append(scope, SPAN)

    response = client.get(LIST, headers=auth)

    assert response.status_code == 200
    assert response.json()["trace_ids"] == [
        {"trace_id": SPAN.trace_id, "span_count": 1, "step_count": 1, "duration_ms": 2.0}
    ]


def test_a_trace_is_queried_by_id(
    client: TestClient, traces: FileTraceStore, scope: Scope, auth: dict[str, str]
) -> None:
    traces.append(scope, SPAN)

    response = client.get(QUERY, params={"trace_id": SPAN.trace_id}, headers=auth)

    assert response.status_code == 200
    body = cast(dict[str, Any], response.json())
    assert body["duration_ms"] == 2.0
    assert [span["span_id"] for span in body["spans"]] == [SPAN.span_id]
    assert body["spans"][0]["duration_ms"] == 2.0


def test_the_list_paginates_by_offset(
    client: TestClient, traces: FileTraceStore, scope: Scope, auth: dict[str, str]
) -> None:
    for index in range(3):
        traces.append(
            scope, Span(new_trace_id(), f"000000000000000{index}", "invoke_agent", 1_000_000)
        )

    response = client.get(LIST, params={"limit": 2, "offset": 1}, headers=auth)

    body = cast(dict[str, Any], response.json())
    assert len(body["trace_ids"]) == 2
    assert (body["total"], body["limit"], body["offset"]) == (3, 2, 1)


@pytest.mark.parametrize("params", [{"limit": "abc"}, {"limit": "-1"}, {"offset": "1.5"}])
def test_a_broken_paging_parameter_is_refused(
    client: TestClient, params: dict[str, str], auth: dict[str, str]
) -> None:
    response = client.get(LIST, params=params, headers=auth)

    assert response.status_code == 400
    assert response.json()["error"] == "bad_query"


def test_a_limit_over_the_ceiling_is_refused_rather_than_cut(
    client: TestClient, auth: dict[str, str]
) -> None:
    """静默截断会让客户端以为拿到的是全部。"""
    response = client.get(LIST, params={"limit": 100_000}, headers=auth)

    assert response.status_code == 400


def test_an_offset_past_the_page_ceiling_is_still_reachable(
    client: TestClient, auth: dict[str, str]
) -> None:
    """页大小有上限，offset 没有 —— 翻到第 400 条不该拿不到。"""
    response = client.get(LIST, params={"offset": 401}, headers=auth)

    body = cast(dict[str, Any], response.json())
    assert response.status_code == 200
    assert body["trace_ids"] == []


def test_querying_without_a_trace_id_is_refused(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get(QUERY, headers=auth).status_code == 400


@pytest.mark.parametrize("path", [LIST, QUERY])
@pytest.mark.parametrize("header", [None, "", "Bearer ", "Basic abc", "Bearer wrong-token"])
def test_a_missing_or_wrong_credential_is_refused(
    client: TestClient, path: str, header: str | None
) -> None:
    headers = {"Authorization": header} if header is not None else {}

    assert client.get(path, headers=headers).status_code == 401


def test_a_refusal_does_not_echo_the_credential(client: TestClient) -> None:
    response = client.get(LIST, headers={"Authorization": "Bearer wrong-token"})

    assert "wrong-token" not in response.text
    assert response.json() == {"error": "unauthorized"}


def test_another_sessions_trace_is_not_visible(
    client: TestClient, traces: FileTraceStore, scope: Scope, auth: dict[str, str]
) -> None:
    traces.append(Scope(scope.user_id, "another-session"), SPAN)

    response = client.get(QUERY, params={"trace_id": SPAN.trace_id}, headers=auth)

    assert response.json()["spans"] == []


def test_a_broken_store_is_a_server_error_not_a_silent_empty(
    config: OathConfig, manager: OfflineManager, auth: dict[str, str]
) -> None:
    """读不出来不能报成「没有 trace」—— 那会把故障说成空态。"""
    with TestClient(
        build_app(_biz(manager, _ExplodingStore()), manager, config),
        raise_server_exceptions=False,
    ) as client:
        response = client.get(LIST, headers=auth)

    assert response.status_code == 500
    assert response.json() == {"error": "internal_error"}


def test_a_blocking_query_does_not_stall_the_event_loop(
    config: OathConfig, manager: OfflineManager, auth: dict[str, str]
) -> None:
    """同进程还有 WebSocket server：查询占了循环，心跳与事件投递就跟着停。

    endpoint 必须是同步的 —— Starlette 只把同步 endpoint 丢线程池，`async def` 的
    endpoint 里那段阻塞调用就直接在事件循环上跑。
    """
    app: Starlette = build_app(_biz(manager, _SlowStore()), manager, config)

    async def main() -> int:
        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.002)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://biz"
        ) as ac:
            response = await ac.get(QUERY, params={"trace_id": "abc"}, headers=auth)
        beat.cancel()
        assert response.status_code == 200
        return ticks

    # 一次查询 100ms。事件循环空转的话这段时间够上百次心跳。
    assert asyncio.run(main()) > 10


# --------------------------------------------------------------------------
# create_session：重开一个已经存在过的 scope
# --------------------------------------------------------------------------


class FakeAgentProcess:
    """`_agent_alive` 只看 `poll()`。返回码非 None 就是不在了。

    `poll` 读的是类上的开关而不是构造时的值 —— 已经登记在表里的那个也要能跟着一起死。
    """

    alive = True

    def poll(self) -> int | None:
        return None if type(self).alive else -9

    def terminate(self) -> None:
        type(self).alive = False

    def wait(self, timeout: float | None = None) -> int | None:
        return self.poll()


class LaunchingManager(SessionManager):
    """登记 session 并挂一个进程替身，不真的监听端口。"""

    def _launch(self, entry: SessionEntry, *, wait: bool = True) -> None:
        entry.agent = FakeAgentProcess()  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _every_agent_is_alive() -> Iterator[None]:
    """`terminate` 会把开关按下去，每个用例都得从「都活着」开始。"""
    FakeAgentProcess.alive = True
    yield
    FakeAgentProcess.alive = True


@pytest.fixture
def launching_client(config: OathConfig) -> Iterator[TestClient]:
    instance = LaunchingManager(config, "")
    yield TestClient(build_app(build_application(config, instance), instance, config))
    instance.close()


def _service() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_omitting_the_session_id_opens_a_new_scope_every_time(launching_client: TestClient) -> None:
    """不给 `session_id` 就是新开 —— 每次一个不同的 scope。"""
    first = launching_client.post("/create_session", json={"user_id": "u1"}, headers=_service())
    second = launching_client.post("/create_session", json={"user_id": "u1"}, headers=_service())

    assert first.status_code == second.status_code == 200
    assert first.json()["session_id"] != second.json()["session_id"]


def test_passing_the_session_id_reopens_that_scope(launching_client: TestClient) -> None:
    """带着 `session_id` 回来拿的是同一个 scope 和一把新凭证。"""
    first = launching_client.post("/create_session", json={"user_id": "u1"}, headers=_service())
    session_id = first.json()["session_id"]
    FakeAgentProcess.alive = False  # agent 崩了，那条记录已经不算「开着」
    try:
        again = launching_client.post(
            "/create_session", json={"user_id": "u1", "session_id": session_id}, headers=_service()
        )
    finally:
        FakeAgentProcess.alive = True

    assert again.status_code == 200
    assert again.json()["session_id"] == session_id, "重开是同一个 scope，不是另开一个"
    assert again.json()["credential"] != first.json()["credential"], "凭证重签，旧的作废"


def test_reopening_a_live_scope_is_a_conflict(launching_client: TestClient) -> None:
    """那边还活着就别换那把凭证 —— 409 告诉客户端接着用原来那条连接。"""
    first = launching_client.post("/create_session", json={"user_id": "u1"}, headers=_service())

    again = launching_client.post(
        "/create_session",
        json={"user_id": "u1", "session_id": first.json()["session_id"]},
        headers=_service(),
    )

    assert again.status_code == 409
    assert again.json()["error"] == "already_open"


def test_a_malformed_session_id_is_a_bad_payload(launching_client: TestClient) -> None:
    response = launching_client.post(
        "/create_session",
        json={"user_id": "u1", "session_id": {"nested": True}},
        headers=_service(),
    )

    assert response.status_code == 400
    assert response.json()["error"] == "bad_payload"
