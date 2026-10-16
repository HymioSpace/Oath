"""真进程：biz 拉起每个 scope 的 agent 与 supervisor，模型走本地替身。

**要真的 Redis。** agent 进程的队列是 Redis Streams，这条链路绕不过去；
连不上就整组跳过，不要拿替身假装跑过。

```
pytest tests/test_service_processes.py            # 自动跳过（没有 Redis）
OATH_TEST_REDIS=redis://127.0.0.1:6379/15 pytest tests/test_service_processes.py
```
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, cast

import httpx
import pytest
from redis import Redis
from websockets.sync.client import connect

TOKEN = "synthetic-integration-token"
REDIS = os.environ.get("OATH_TEST_REDIS", "redis://127.0.0.1:6379/15")
SERVICE = {"Authorization": f"Bearer {TOKEN}"}


def redis_reachable() -> bool:
    client = Redis.from_url(REDIS, socket_connect_timeout=0.5)
    try:
        return bool(client.ping())
    except Exception:
        return False
    finally:
        client.close()


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not redis_reachable(), reason="需要真 Redis：队列是 Redis Streams"),
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def eventually(check: Callable[[], Any], timeout: float = 20) -> Any:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except Exception as exc:
            last = exc
        time.sleep(0.05)
    raise AssertionError(f"等待状态变化超时；最后一次错误：{last}")


class SyntheticModel(BaseHTTPRequestHandler):
    """一个只会说 hello 的 SSE 端点。记下收到过几次请求。"""

    requests: ClassVar[list[Any]] = []

    def do_POST(self) -> None:  # BaseHTTPRequestHandler 约定的名字
        SyntheticModel.requests.append(
            json.loads(self.rfile.read(int(self.headers["content-length"])))
        )
        chunks = [
            {
                "id": "synthetic-response",
                "model": "synthetic",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "hello"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "synthetic-response",
                "model": "synthetic",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1},
            },
        ]
        body = (
            "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: Any) -> None:
        pass


@pytest.fixture
def services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """模型端点 + 配置 + 真的 Redis key 前缀。每条测试用独立的 db 前缀。"""
    SyntheticModel.requests = []
    model = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticModel)
    worker = threading.Thread(target=model.serve_forever, daemon=True)
    worker.start()
    extension = tmp_path / "extensions" / "cli"
    extension.mkdir(parents=True)
    (extension / "identity.py").write_text("""import os
from core.protocol.datatype.cli import CliCommand

def identity(*, scope, user_context):
    return {"user_id": scope.user_id, "context": user_context}

COMMANDS = [CliCommand("identity", "synthetic process probe", {"type": "object"}, identity)]
""")
    config = tmp_path / "oath.toml"
    config.write_text(
        f'''data_root = "{tmp_path / "data"}"
# 必须写在任何 [表头] 之前 —— TOML 会把它归到上一张表里去。
service_credentials = ["{TOKEN}"]
[server]
http_port = {free_port()}
[middleware]
redis_url = "{REDIS}"
[manager]
port_range_start = {free_port()}
port_range_end = {free_port() + 20}
reap_interval_seconds = 0.3
idle_grace_seconds = 1.0
[agent]
extension_dirs = ["{extension.parent}"]
skill_dir = "{tmp_path / "skills"}"
workspace_root = "{tmp_path}"
[agent.provider]
model = "synthetic"
api_base = "http://127.0.0.1:{model.server_port}/v1"
api_key_env = "OATH_SYNTHETIC_KEY"
[supervisor]
heartbeat_interval_seconds = 0.4
heartbeat_timeout_seconds = 0.2
heartbeat_failures_before_restart = 2
invoke_interval_seconds = 0.3
restart_backoff_seconds = 0.1
restart_backoff_cap_seconds = 0.5
'''
    )
    monkeypatch.setenv("OATH_SYNTHETIC_KEY", "synthetic-local-key")
    try:
        yield config
    finally:
        model.shutdown()
        model.server_close()
        worker.join(timeout=2)


def base(config: Path) -> str:
    return f"http://127.0.0.1:{tomllib.loads(config.read_text())['server']['http_port']}"


def command(ws: Any, type_: str, id_: str, session_id: str, payload: dict[str, Any] | None) -> None:
    ws.send(
        json.dumps(
            {"v": 1, "type": type_, "id": id_, "session_id": session_id, "payload": payload or {}}
        )
    )


def open_session(ws: Any, id_: str, issued: dict[str, Any]) -> None:
    """`session.open` **不带 `session_id`** —— 要绑定的正是它，带上就是自相矛盾。"""
    ws.send(
        json.dumps(
            {
                "v": 1,
                "type": "session.open",
                "id": id_,
                "payload": {"credential": issued["credential"]},
            }
        )
    )


def receive(ws: Any, matches: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    deadline = time.monotonic() + 15
    while True:
        frame = json.loads(ws.recv(timeout=max(0.01, deadline - time.monotonic())))
        if matches(frame):
            return cast(dict[str, Any], frame)


def reconnect(url: str, timeout: float = 20) -> Any:
    """连上就返回，连不上就重试到超时。

    **agent 崩过之后，端口有一小段没人听。** manager 只负责把 agent 拉起来，不等它开始
    监听 —— 等的那个位置在客户端：重连本来就是它的事。一次 refused 就判失败，等于把一次
    崩溃当成永久下线。
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        try:
            return connect(url, open_timeout=max(0.1, min(2.0, remaining)))
        except OSError:
            if remaining <= 0:
                raise
            time.sleep(0.05)


def agent_pid(biz: subprocess.Popen[bytes]) -> int | None:
    """biz 拉起来的 agent 子进程。"""
    rows = subprocess.check_output(["ps", "-axo", "pid=,ppid=,command="], text=True)
    for row in rows.splitlines():
        pid, ppid, args = row.strip().split(None, 2)
        if int(ppid) == biz.pid and "bootstrap.agent.process" in args:
            return int(pid)
    return None


@pytest.fixture
def biz(services: Path) -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(
        [sys.executable, "-m", "bootstrap.biz.process", "--config", str(services)],
        stdin=subprocess.DEVNULL,
    )
    try:
        eventually(lambda: httpx.get(base(services) + "/heartbeat", timeout=0.5).status_code == 200)
        yield process
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def create_session(config: Path, user_id: str = "u1") -> dict[str, Any]:
    response = httpx.post(
        base(config) + "/create_session", json={"user_id": user_id}, headers=SERVICE, timeout=10
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def session_auth(issued: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {issued['credential']}"}


def test_a_session_answers_and_comes_back_after_the_agent_is_killed(
    biz: subprocess.Popen[bytes], services: Path
) -> None:
    issued = create_session(services)
    session_id = cast(str, issued["session_id"])
    url = cast(str, issued["ws_url"])

    with connect(url, open_timeout=5) as ws:
        open_session(ws, "open", issued)
        receive(ws, lambda f: f.get("id") == "open")
        command(
            ws,
            "user.message",
            "message",
            session_id,
            {"content": "hi", "user_context": {"city": "synthetic"}},
        )
        # ACK 只说明队列收到了，回答要等 Brain 走完一轮。
        receive(ws, lambda f: f.get("event_type") == "assistant.message")

    first = eventually(lambda: agent_pid(biz))
    os.kill(first, signal.SIGKILL)
    # supervisor 发现 agent 不在了请 manager 重来，端口不变 —— 客户端那把凭证也不变。
    second = eventually(lambda: (pid := agent_pid(biz)) is not None and pid != first and pid)

    with reconnect(url) as ws:
        open_session(ws, "open2", issued)
        receive(ws, lambda f: f.get("id") == "open2")
        command(ws, "session.status", "status", session_id, None)
        state = receive(ws, lambda f: f.get("event_type") == "session.status")["payload"]

    # 换了一个进程，那一轮对话仍从 journal 里读得回来。
    assert second != first
    assert any(
        m.get("role") == "assistant" and m.get("content") == "hello" for m in state["messages"]
    )
    assert len(SyntheticModel.requests) == 1


def test_biz_proxies_a_command_to_the_agent_that_owns_the_session(
    biz: subprocess.Popen[bytes], services: Path
) -> None:
    """CLI 跑在 biz，user_context 要向那个 scope 的 agent 要 —— 两边对得上才算通。"""
    issued = create_session(services)
    session_id = cast(str, issued["session_id"])
    with connect(cast(str, issued["ws_url"]), open_timeout=5) as ws:
        open_session(ws, "open", issued)
        receive(ws, lambda f: f.get("id") == "open")
        command(
            ws,
            "user.message",
            "message",
            session_id,
            {"content": "hi", "user_context": {"city": "synthetic"}},
        )
        receive(ws, lambda f: f.get("event_type") == "assistant.message")

    response = httpx.post(
        base(services) + "/cli/identity", json={}, headers=session_auth(issued), timeout=10
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"user_id": "u1", "context": {"city": "synthetic"}}


def test_a_scope_is_reaped_once_its_connection_count_stays_zero(
    biz: subprocess.Popen[bytes], services: Path
) -> None:
    issued = create_session(services)
    session_id = cast(str, issued["session_id"])
    with connect(cast(str, issued["ws_url"]), open_timeout=5) as ws:
        open_session(ws, "open", issued)
        receive(ws, lambda f: f.get("id") == "open")

    def listed() -> list[Any]:
        rows: list[Any] = httpx.get(
            base(services) + "/list_sessions", headers=SERVICE, timeout=5
        ).json()["sessions"]
        return rows

    assert [row["session_id"] for row in listed()] == [session_id]

    # 连接断了，冷却期一过 manager 就回收这个 scope 的两个进程。
    eventually(lambda: not listed() and agent_pid(biz) is None)
