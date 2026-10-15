"""`oath-cli` 这个可执行文件：参数怎么变成请求，失败怎么变成退出码。

不打真网络 —— `urlopen` 换掉，端点、请求头、query string 都看得见。要验的正是
「执行环境里那条命令打出去的请求对不对」，不是 Oath server 的行为（那在
`test_cli_server.py`）。
"""

from __future__ import annotations

import email.message
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.error import HTTPError

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "oath-cli"
SERVER = "http://127.0.0.1:8766"
TOKEN = "synthetic-test-token"

BODY = b'{"error": "bad_query"}'

LISTING = {
    "commands": [
        {
            "name": "search",
            "description": "Search the knowledge base.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "关键词"},
                    "limit": {"type": "integer", "description": "最多几条"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        }
    ]
}


def load_cli() -> ModuleType:
    """按路径导入 —— 它没有 .py 后缀，是给执行环境用的可执行文件。"""
    loader = importlib.machinery.SourceFileLoader("oath_cli", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class _Response:
    """`urlopen` 的返回值要够用：能当上下文管理器，能读出正文。"""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = load_cli()
    monkeypatch.setenv("OATH_CLI_SERVER", SERVER)
    monkeypatch.setenv("OATH_CLI_TOKEN", TOKEN)
    return module


@pytest.fixture
def asked(cli: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """替掉 `urlopen`，把每次请求按顺序记下来，按 path 回响应。"""
    seen: list[Any] = []

    def fake_urlopen(request: Any, timeout: float = 0.0) -> _Response:
        seen.append(request)
        body = LISTING if request.full_url.endswith("/cli_commands") else {"hits": []}
        return _Response(json.dumps(body).encode())

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)
    return seen


def sent(asked: list[Any]) -> Any:
    """第二次请求才是命令本身（第一次是查命令表）。"""
    return asked[1]


def test_a_positional_argument_lands_on_the_first_declared_parameter(
    cli: ModuleType, asked: list[Any]
) -> None:
    """`oath-cli search 工作区` 里 `工作区` 是 query —— 位置就是声明顺序。"""
    cli.main(["search", "工作区"])

    assert json.loads(sent(asked).data.decode()) == {"query": "工作区"}


def test_a_named_argument_does_not_take_up_a_position(cli: ModuleType, asked: list[Any]) -> None:
    """`--limit=2 工作区`：limit 写了自己的名字，位置留给 query。"""
    cli.main(["search", "--limit=2", "工作区"])

    assert json.loads(sent(asked).data.decode()) == {"query": "工作区", "limit": "2"}


def test_the_command_is_posted_with_the_arguments_in_the_body(
    cli: ModuleType, asked: list[Any]
) -> None:
    """命令是动作 —— GET 的缓存与重试语义不对。"""
    cli.main(["search", "工作区"])

    assert (sent(asked).get_method(), sent(asked).full_url) == ("POST", f"{SERVER}/cli/search")


def test_the_credential_from_the_environment_goes_into_the_request(
    cli: ModuleType, asked: list[Any]
) -> None:
    """脚本不判断 credential 有没有用 —— 它只是把它原样带过去。"""
    cli.main(["search", "工作区"])

    assert sent(asked).get_header("Authorization") == f"Bearer {TOKEN}"


def test_running_it_without_arguments_lists_the_commands(
    cli: ModuleType, asked: list[Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([]) == 0

    out = capsys.readouterr().out
    assert "search" in out
    assert "query（必填）" in out


def test_an_unknown_command_is_a_usage_error(cli: ModuleType, asked: list[Any]) -> None:
    with pytest.raises(SystemExit) as exit:
        cli.main(["nope"])

    assert exit.value.code == cli.EXIT_USAGE


def test_an_unknown_parameter_is_a_usage_error(cli: ModuleType, asked: list[Any]) -> None:
    with pytest.raises(SystemExit) as exit:
        cli.payload_of(["search", "--fields=all"])

    assert exit.value.code == cli.EXIT_USAGE


def test_more_positional_arguments_than_the_command_takes(
    cli: ModuleType, asked: list[Any]
) -> None:
    with pytest.raises(SystemExit) as exit:
        cli.payload_of(["search", "工作区", "5", "多余"])

    assert exit.value.code == cli.EXIT_USAGE


def test_a_rejected_request_fails_with_the_body_the_server_gave(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """命令自己知道错在哪：正文就是错误说明，原样打出去。"""

    def refuse(request: Any, timeout: float = 0.0) -> _Response:
        if request.full_url.endswith("/cli_commands"):
            return _Response(json.dumps(LISTING).encode())
        assert request.full_url.endswith("/cli/search")
        raise HTTPError(
            request.full_url, 400, "Bad Request", email.message.Message(), io.BytesIO(BODY)
        )

    monkeypatch.setattr(cli, "urlopen", refuse)

    with pytest.raises(SystemExit) as exit:
        cli.main(["search", "工作区"])

    assert exit.value.code == cli.EXIT_FAILED
    assert "bad_query" in capsys.readouterr().err


def test_without_a_server_to_call_it_refuses_to_run(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """在执行环境外直接跑它没有端点 —— 说清楚，不要连一个猜出来的主机。"""
    monkeypatch.delenv("OATH_CLI_SERVER", raising=False)

    with pytest.raises(SystemExit) as exit:
        cli.main(["search", "工作区"])

    assert exit.value.code == cli.EXIT_USAGE
    assert "OATH_CLI_SERVER" in capsys.readouterr().err
