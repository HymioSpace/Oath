"""CLI server：命令表、参数校验、身份，以及 user_context 到命令手上的那一步。

传输层在这里测 —— 命令本身的语义在 `test_cli_search.py`。用真实装配与 `TestClient`：
不起真 server，要验的正是「从 HTTP 打进去，命令拿到的就是该拿到的东西」。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from starlette.testclient import TestClient

from biz.cli import cli_server
from biz.cli.cli_server import CliCommand
from bootstrap.biz_server import build_app
from bootstrap.runtime import Runtime
from core.context.user_context import TTL_SECONDS
from core.protocol.datatype.config import OathConfig
from core.protocol.datatype.scope import Scope
from core.utils.encoding import JsonObject
from tests._support.intake_fixtures import ScriptedProvider

TOKEN = "synthetic-test-token"
SCOPE = Scope("u1", "s1")
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    instance = Runtime(
        OathConfig(data_root=tmp_path, credentials={TOKEN: (SCOPE.user_id, SCOPE.session_id)}),
        loop=asyncio.new_event_loop(),
        provider=ScriptedProvider(),
    )
    yield instance
    instance.close()


@pytest.fixture
def client(runtime: Runtime) -> TestClient:
    return TestClient(build_app(runtime))


def test_the_command_table_carries_the_names_and_the_parameters(client: TestClient) -> None:
    """`oath-cli` 不带参数时靠它列用法 —— 位置参数的顺序就在参数声明里。"""
    response = client.get("/cli_commands")

    [entry] = response.json()["commands"]
    assert entry["name"] == "search"
    assert list(entry["parameters"]["properties"]) == ["query", "limit"]
    assert entry["parameters"]["required"] == ["query"]


def run(client: TestClient, name: str, **body: Any) -> Any:
    return client.post(f"/cli/{name}", json=body, headers=AUTH)


def test_a_command_runs_with_the_arguments_it_was_given(client: TestClient) -> None:
    response = run(client, "search", query="工作区")

    body = response.json()
    assert response.status_code == 200
    assert body["query"] == "工作区"
    assert [hit["title"] for hit in body["hits"]]


def test_a_number_in_the_body_arrives_as_a_number(client: TestClient) -> None:
    """body 是 JSON，类型本来就带着 —— 不必在服务端猜。"""
    response = run(client, "search", query="工作区", limit=1)

    assert response.status_code == 200
    assert len(response.json()["hits"]) == 1


def test_an_argument_from_the_command_line_arrives_as_the_type_it_declares(
    client: TestClient,
) -> None:
    """`oath-cli --limit=2` 过来的是字符串 `"2"` —— 声明成整数就得按整数收。"""
    response = client.post("/cli/search", json={"query": "工作区", "limit": "1"}, headers=AUTH)

    assert response.status_code == 200
    assert len(response.json()["hits"]) == 1


def test_a_command_is_executed_with_post_not_get(client: TestClient) -> None:
    """命令是动作。GET 的缓存与重试语义不对，将来代理写操作会出事。"""
    assert client.get("/cli/search", params={"query": "工作区"}, headers=AUTH).status_code == 405


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({}, "少参数"),
        ({"query": "x", "fields": "all"}, "多参数"),
        ({"query": "x", "limit": "三条"}, "类型不对"),
    ],
)
def test_arguments_the_command_cannot_accept_are_refused(
    client: TestClient, body: dict[str, Any], reason: str
) -> None:
    """静默忽略的话，命令拼错参数名会跑出一个「成功但答非所问」的结果。"""
    response = run(client, "search", **body)

    assert response.status_code == 400, reason
    assert response.json()["error"] == "bad_query"


def test_a_body_that_is_not_json_is_refused(client: TestClient) -> None:
    response = client.post(
        "/cli/search", content=b"query=x", headers={**AUTH, "Content-Type": "text/plain"}
    )

    assert response.status_code == 400


def test_a_request_without_a_credential_is_refused(client: TestClient) -> None:
    assert client.post("/cli/search", json={"query": "x"}).status_code == 401


def test_a_command_that_does_not_exist_is_not_found(client: TestClient) -> None:
    """认不出的命令名是「没这个能力」，不是「没这个身份」。"""
    response = run(client, "nope", query="x")

    assert response.status_code == 404
    assert response.json()["error"] == "unknown_command"


def test_a_command_that_fails_is_reported_as_a_server_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(**_: Any) -> Any:
        raise RuntimeError("业务炸了")

    monkeypatch.setattr(
        cli_server, "COMMANDS", (CliCommand("boom", "always fails", {"type": "object"}, boom),)
    )

    response = run(client, "boom")

    assert response.status_code == 500
    assert response.json()["error"] == "command_failed"


# -- user_context 到命令手上 -------------------------------------------------


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> list[JsonObject]:
    """一条把收到的参数原样交出来的替身命令。"""
    seen: list[JsonObject] = []

    def handler(**arguments: Any) -> Any:
        seen.append(arguments)
        return {"ok": True}

    monkeypatch.setattr(
        cli_server,
        "COMMANDS",
        (
            CliCommand(
                name="probe",
                description="echo what the framework handed over",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                handler=handler,
            ),
        ),
    )
    return seen


def test_the_command_receives_the_scope_it_was_called_for(
    client: TestClient, probe: list[JsonObject]
) -> None:
    """HTTP 上没有 session —— 身份只能从 credential 反查出来的那一个。"""
    run(client, "probe", query="x")

    assert cast(object, probe[0]["scope"]) == SCOPE


def test_the_command_receives_that_scope_s_user_context(
    client: TestClient, runtime: Runtime, probe: list[JsonObject]
) -> None:
    """透传：框架不解释这份 kv，也不拿它判权限。"""
    runtime.ctx.store.transact(
        SCOPE, "test.seed", lambda s: s["meta"].update(user_context=_table(time.time()))
    )

    run(client, "probe", query="x")

    assert probe[0]["user_context"] == {"project": "oath", "env": "dev"}


def test_an_expired_key_never_reaches_the_command(
    client: TestClient, runtime: Runtime, probe: list[JsonObject]
) -> None:
    """过期的在读的时候裁掉 —— 命令拿到的是当下还成立的 kv，不是历史。"""
    stale = time.time() - TTL_SECONDS - 1
    runtime.ctx.store.transact(
        SCOPE,
        "test.seed",
        lambda s: s["meta"].update(user_context={"gone": {"value": "旧", "at": stale}}),
    )

    run(client, "probe", query="x")

    assert probe[0]["user_context"] == {}


def _table(now: float) -> JsonObject:
    """两个还成立的 key。"""
    return {
        "project": {"value": "oath", "at": now - 60},
        "env": {"value": "dev", "at": now - 120},
    }
