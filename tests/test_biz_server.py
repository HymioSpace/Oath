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

from bootstrap.biz_server import build_app
from bootstrap.runtime import Runtime
from core.protocol.datatype.config import OathConfig
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.trace import Span, SpanStatus, new_trace_id
from core.protocol.interface.trace import TraceStore
from tests._support.intake_fixtures import ScriptedProvider

TOKEN = "synthetic-test-token"
SCOPE = Scope("u1", "s1")
AUTH = {"Authorization": f"Bearer {TOKEN}"}
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


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    instance = Runtime(
        OathConfig(
            data_root=tmp_path,
            credentials={TOKEN: (SCOPE.user_id, SCOPE.session_id)},
        ),
        loop=asyncio.new_event_loop(),
        provider=ScriptedProvider(),
    )
    yield instance
    instance.close()


@pytest.fixture
def client(runtime: Runtime) -> TestClient:
    return TestClient(build_app(runtime))


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


def test_a_credential_carries_the_scope(client: TestClient, runtime: Runtime) -> None:
    runtime.traces.append(SCOPE, SPAN)

    response = client.get(LIST, headers=AUTH)

    assert response.status_code == 200
    assert response.json()["trace_ids"] == [
        {"trace_id": SPAN.trace_id, "span_count": 1, "step_count": 1, "duration_ms": 2.0}
    ]


def test_a_trace_is_queried_by_id(client: TestClient, runtime: Runtime) -> None:
    runtime.traces.append(SCOPE, SPAN)

    response = client.get(QUERY, params={"trace_id": SPAN.trace_id}, headers=AUTH)

    assert response.status_code == 200
    body = cast(dict[str, Any], response.json())
    assert body["duration_ms"] == 2.0
    assert [span["span_id"] for span in body["spans"]] == [SPAN.span_id]
    assert body["spans"][0]["duration_ms"] == 2.0


def test_the_list_paginates_by_offset(client: TestClient, runtime: Runtime) -> None:
    for index in range(3):
        runtime.traces.append(
            SCOPE, Span(new_trace_id(), f"000000000000000{index}", "invoke_agent", 1_000_000)
        )

    response = client.get(LIST, params={"limit": 2, "offset": 1}, headers=AUTH)

    body = cast(dict[str, Any], response.json())
    assert len(body["trace_ids"]) == 2
    assert (body["total"], body["limit"], body["offset"]) == (3, 2, 1)


@pytest.mark.parametrize("params", [{"limit": "abc"}, {"limit": "-1"}, {"offset": "1.5"}])
def test_a_broken_paging_parameter_is_refused(client: TestClient, params: dict[str, str]) -> None:
    response = client.get(LIST, params=params, headers=AUTH)

    assert response.status_code == 400
    assert response.json()["error"] == "bad_query"


def test_a_limit_over_the_ceiling_is_refused_rather_than_cut(client: TestClient) -> None:
    """静默截断会让客户端以为拿到的是全部。"""
    response = client.get(LIST, params={"limit": 100_000}, headers=AUTH)

    assert response.status_code == 400


def test_an_offset_past_the_page_ceiling_is_still_reachable(client: TestClient) -> None:
    """页大小有上限，offset 没有 —— 翻到第 400 条不该拿不到。"""
    response = client.get(LIST, params={"offset": 401}, headers=AUTH)

    body = cast(dict[str, Any], response.json())
    assert response.status_code == 200
    assert body["trace_ids"] == []


def test_querying_without_a_trace_id_is_refused(client: TestClient) -> None:
    assert client.get(QUERY, headers=AUTH).status_code == 400


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


def test_another_sessions_trace_is_not_visible(client: TestClient, runtime: Runtime) -> None:
    runtime.traces.append(Scope("u1", "s2"), SPAN)

    response = client.get(QUERY, params={"trace_id": SPAN.trace_id}, headers=AUTH)

    assert response.json()["spans"] == []


def test_a_broken_store_is_a_server_error_not_a_silent_empty(runtime: Runtime) -> None:
    """读不出来不能报成「没有 trace」—— 那会把故障说成空态。"""
    runtime.traces = _ExplodingStore()

    with TestClient(build_app(runtime), raise_server_exceptions=False) as client:
        response = client.get(LIST, headers=AUTH)

    assert response.status_code == 500
    assert response.json() == {"error": "internal_error"}


def test_a_blocking_query_does_not_stall_the_event_loop(runtime: Runtime) -> None:
    """同进程还有 WebSocket server：查询占了循环，心跳与事件投递就跟着停。

    endpoint 必须是同步的 —— Starlette 只把同步 endpoint 丢线程池，`async def` 的
    endpoint 里那段阻塞调用就直接在事件循环上跑。
    """
    runtime.traces = _SlowStore()
    app: Starlette = build_app(runtime)

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
            response = await ac.get(QUERY, params={"trace_id": "abc"}, headers=AUTH)
        beat.cancel()
        assert response.status_code == 200
        return ticks

    # 一次查询 100ms。事件循环空转的话这段时间够上百次心跳。
    assert asyncio.run(main()) > 10
