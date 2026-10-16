"""CLI server：命令表、参数校验、身份，以及 user_context 到命令手上的那一步。

传输层在这里测 —— 命令本身在 `test_cli_echo.py`。用真实装配与 `TestClient`：
不起真 server，要验的正是「从 HTTP 打进去，命令拿到的就是该拿到的东西」。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from starlette.testclient import TestClient

from biz.application import BizApplication
from bootstrap.biz.assembly import build_application
from bootstrap.biz.server import build_app
from bootstrap.clients.agent_client import AgentClient
from core.protocol.datatype import CliCommand
from core.protocol.datatype.config import OathConfig
from core.protocol.datatype.scope import Scope
from core.protocol.interface.agent_access import AgentUnavailable
from core.utils.encoding import JsonObject
from tests.test_service_boundaries import OfflineManager

TOKEN = "synthetic-service-token"

#: 只扫仓库里那一份。缺省配置的第二个目录指向开发者本机的 `extensions-local/`，
#: 测试不能读它。
COMMITTED = Path(__file__).resolve().parents[1] / "extensions"

#: 带一个整型参数的声明 —— 类型这件事得有两个参数才验得了。
SIZED: JsonObject = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "limit": {"type": "integer"}},
    "required": ["text"],
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def no_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """装一份空的 user_context —— agent 那一跳不归这个文件管。"""
    monkeypatch.setattr(AgentClient, "user_context", lambda self, scope: {})


@pytest.fixture
def config(tmp_path: Path) -> OathConfig:
    return replace(
        OathConfig(data_root=tmp_path, service_credentials=(TOKEN,)),
        agent=replace(OathConfig().agent, extension_dirs=(COMMITTED,)),
    )


@pytest.fixture
def manager(config: OathConfig) -> Iterator[OfflineManager]:
    """登记 session 但不拉进程 —— 这里验的是传输层。"""
    instance = OfflineManager(config, "")
    yield instance
    instance.close()


@pytest.fixture
def session(manager: OfflineManager) -> tuple[Scope, str]:
    issued = manager.create_session("u1")
    return Scope("u1", str(issued["session_id"])), str(issued["credential"])


@pytest.fixture
def scope(session: tuple[Scope, str]) -> Scope:
    return session[0]


@pytest.fixture
def auth(session: tuple[Scope, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {session[1]}"}


@pytest.fixture
def app(config: OathConfig, manager: OfflineManager) -> BizApplication:
    return build_application(config, manager)


@pytest.fixture
def client(app: BizApplication, manager: OfflineManager, config: OathConfig) -> TestClient:
    return TestClient(build_app(app, manager, config))


def command(
    app: BizApplication, monkeypatch: pytest.MonkeyPatch, name: str, parameters: JsonObject
) -> list[JsonObject]:
    """换掉命令表，装一条把收到的参数记下来的替身命令。

    返回记录表 —— 断言看的是「框架交了什么」，不是命令自己答了什么。
    """
    seen: list[JsonObject] = []

    def handler(**arguments: Any) -> Any:
        seen.append(arguments)
        return {"ok": True}

    monkeypatch.setattr(
        app,
        "commands",
        (
            CliCommand(
                name=name, description=f"{name} stand-in", parameters=parameters, handler=handler
            ),
        ),
    )
    return seen


def test_the_command_table_carries_the_names_and_the_parameters(
    client: TestClient, auth: dict[str, str]
) -> None:
    """`oath-cli` 不带参数时靠它列用法 —— 位置参数的顺序就在参数声明里。

    这条命令是从扩展目录扫出来的：它能出现在这里，就说明目录发现接上了。
    """
    response = client.get("/cli_commands", headers=auth)

    [entry] = response.json()["commands"]
    assert entry["name"] == "echo"
    assert list(entry["parameters"]["properties"]) == ["text"]
    assert entry["parameters"]["required"] == ["text"]


def run(client: TestClient, auth: dict[str, str], name: str, **body: Any) -> Any:
    return client.post(f"/cli/{name}", json=body, headers=auth)


def test_a_command_runs_with_the_arguments_it_was_given(
    client: TestClient, auth: dict[str, str]
) -> None:
    response = run(client, auth, "echo", text="工作区")

    assert response.status_code == 200
    assert response.json() == {"text": "工作区"}


def test_a_number_in_the_body_arrives_as_a_number(
    client: TestClient,
    app: BizApplication,
    monkeypatch: pytest.MonkeyPatch,
    auth: dict[str, str],
) -> None:
    """body 是 JSON，类型本来就带着 —— 不必在服务端猜。"""
    sized = command(app, monkeypatch, "sized", SIZED)

    response = run(client, auth, "sized", text="工作区", limit=1)

    assert response.status_code == 200
    assert sized[0]["limit"] == 1


def test_an_argument_from_the_command_line_arrives_as_the_type_it_declares(
    client: TestClient,
    app: BizApplication,
    monkeypatch: pytest.MonkeyPatch,
    auth: dict[str, str],
) -> None:
    """`oath-cli --limit=2` 过来的是字符串 `"2"` —— 声明成整数就得按整数收。"""
    sized = command(app, monkeypatch, "sized", SIZED)

    response = client.post("/cli/sized", json={"text": "工作区", "limit": "1"}, headers=auth)

    assert response.status_code == 200
    assert sized[0]["limit"] == 1


def test_a_command_is_executed_with_post_not_get(client: TestClient, auth: dict[str, str]) -> None:
    """命令是动作。GET 的缓存与重试语义不对，将来代理写操作会出事。"""
    assert client.get("/cli/echo", params={"text": "工作区"}, headers=auth).status_code == 405


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({}, "少参数"),
        ({"text": "x", "fields": "all"}, "多参数"),
    ],
)
def test_arguments_the_command_cannot_accept_are_refused(
    client: TestClient, body: dict[str, Any], reason: str, auth: dict[str, str]
) -> None:
    """静默忽略的话，命令拼错参数名会跑出一个「成功但答非所问」的结果。"""
    response = run(client, auth, "echo", **body)

    assert response.status_code == 400, reason
    assert response.json()["error"] == "bad_query"


def test_an_argument_whose_text_is_not_the_declared_type_is_refused(
    client: TestClient,
    app: BizApplication,
    monkeypatch: pytest.MonkeyPatch,
    auth: dict[str, str],
) -> None:
    command(app, monkeypatch, "sized", SIZED)

    response = run(client, auth, "sized", text="x", limit="三条")

    assert response.status_code == 400
    assert response.json()["error"] == "bad_query"


def test_a_body_that_is_not_json_is_refused(client: TestClient, auth: dict[str, str]) -> None:
    response = client.post(
        "/cli/echo", content=b"text=x", headers={**auth, "Content-Type": "text/plain"}
    )

    assert response.status_code == 400


def test_a_request_without_a_credential_is_refused(client: TestClient) -> None:
    assert client.post("/cli/echo", json={"text": "x"}).status_code == 401


def test_a_command_that_does_not_exist_is_not_found(
    client: TestClient, auth: dict[str, str]
) -> None:
    """认不出的命令名是「没这个能力」，不是「没这个身份」。"""
    response = run(client, auth, "nope", text="x")

    assert response.status_code == 404
    assert response.json()["error"] == "unknown_command"


def test_a_command_that_fails_is_reported_as_a_server_error(
    client: TestClient,
    app: BizApplication,
    monkeypatch: pytest.MonkeyPatch,
    auth: dict[str, str],
) -> None:
    def boom(**_: Any) -> Any:
        raise RuntimeError("业务炸了")

    monkeypatch.setattr(
        app,
        "commands",
        (CliCommand("boom", "always fails", {"type": "object"}, boom),),
    )

    response = run(client, auth, "boom")

    assert response.status_code == 500
    assert response.json()["error"] == "command_failed"


# -- user_context 到命令手上 -------------------------------------------------


@pytest.fixture
def probe(app: BizApplication, monkeypatch: pytest.MonkeyPatch) -> list[JsonObject]:
    """一条把收到的参数原样交出来的替身命令。"""
    return command(
        app,
        monkeypatch,
        "probe",
        {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )


def test_the_command_receives_the_scope_it_was_called_for(
    client: TestClient, probe: list[JsonObject], auth: dict[str, str], scope: Scope
) -> None:
    """HTTP 上没有 session —— 身份只能从 credential 反查出来的那一个。"""
    run(client, auth, "probe", text="x")

    assert cast(object, probe[0]["scope"]) == scope


def test_the_command_receives_the_context_the_agent_reports(
    client: TestClient,
    probe: list[JsonObject],
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """透传：biz 不解释这份 kv，也不拿它判权限。"""
    monkeypatch.setattr(
        AgentClient, "user_context", lambda self, scope: {"project": "oath", "env": "dev"}
    )

    run(client, auth, "probe", text="x")

    assert probe[0]["user_context"] == {"project": "oath", "env": "dev"}


def test_an_unreachable_agent_is_reported_not_swallowed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth: dict[str, str]
) -> None:
    """拿不到 user_context 却把命令跑起来，命令就是在缺依据的情况下动手。"""

    def gone(self: AgentClient, scope: Scope) -> JsonObject:
        raise AgentUnavailable("agent down")

    monkeypatch.setattr(AgentClient, "user_context", gone)

    response = run(client, auth, "echo", text="工作区")

    assert response.status_code == 503
    assert response.json()["error"] == "agent_unavailable"
