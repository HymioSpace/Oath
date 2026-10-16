"""scope 的增删。

manager 维护一张 session 表，每个 scope 一对进程：一个 agent、一个 supervisor。
它自己不发输入也不看推理 —— 只做四件事：发凭证、分配端口、拉起、按连接数回收。

**回收只看连接数。** agent 的心跳帧带当前在线连接数；连续为 0 且过了冷却期，就优雅停掉
这两个进程，把目录留在原地 —— journal 与 buffer 都是重启能读的东西，下次有人来
`create_session` 同一个 session_id 就接着跑。

**manager 只管拉起和回收，不重启。** agent 中途死了没有人重新拉它，那个 scope 会一直空着
直到被按连接数回收；要接着用只能重新 `create_session`。
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from bootstrap.common.authentication import SESSION_CREDENTIAL_ENV
from core.protocol.datatype.config import OathConfig
from core.protocol.datatype.scope import Scope, is_valid_scope_id
from core.protocol.error import PermissionDeniedError, WireProtocolError
from core.protocol.interface.session import SessionAuthenticator, SessionIdentity
from core.utils.encoding import JsonObject

_log = logging.getLogger("oath.manager")

#: 端口分配失败重试几次。区间被别的进程占了是常事，不是配置错误。
_PORT_TRIES = 8


@dataclass
class SessionEntry:
    """一个 scope 的进程组。"""

    scope: Scope
    port: int
    credential: str
    agent: subprocess.Popen[bytes] | None = None
    #: `BaseProcess` 而不是 `Process` —— 具体是哪个子类由 start method 决定，注解只
    #: 承诺「一个子进程」。
    supervisor: multiprocessing.process.BaseProcess | None = None
    idle_since: float | None = None
    connections: int = 0

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"


class SessionLimitReached(Exception):
    """已经到 `max_sessions` 了。"""


class SessionManager(SessionAuthenticator):
    """跑在 biz 进程里。**它拥有 agent 与 supervisor 这两个进程的生命周期。**

    它也是 session 级凭证的唯一签发者与唯一持有者 —— 认不出它签的凭证，等于这个
    session 已经不在了。
    """

    def __init__(self, config: OathConfig, config_path: str) -> None:
        self._config = config
        self._config_path = str(Path(config_path).resolve())
        self._sessions: dict[Scope, SessionEntry] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None
        self._context = multiprocessing.get_context("spawn")

    # -- 对外 --------------------------------------------------------------

    def create_session(self, user_id: str) -> JsonObject:
        """给一个用户开一个新 session：签凭证、分端口、拉起两个进程。

        **满了就报失败，不排队。** 排队意味着调用方要处理「等多久」这件事，而它拿到
        返回时那个 scope 未必还空着。
        """
        if not is_valid_scope_id(user_id):
            raise WireProtocolError("user_id 不合法", code="bad_payload")
        with self._lock:
            if len(self._sessions) >= self._config.manager.max_sessions:
                raise SessionLimitReached(
                    f"同时存在的 session 已达上限 {self._config.manager.max_sessions}"
                )
            scope = Scope(user_id, uuid.uuid4().hex[:16])
            port = self._allocate_port()
            entry = SessionEntry(scope, port, secrets.token_urlsafe(32))
            self._sessions[scope] = entry
        try:
            self._launch(entry)
        except Exception:
            with self._lock:
                self._sessions.pop(scope, None)
            raise
        return {
            "user_id": scope.user_id,
            "session_id": scope.session_id,
            "ws_url": entry.url,
            "credential": entry.credential,
        }

    def session_url(self, scope: Scope) -> str:
        entry = self._sessions.get(scope)
        if entry is None:
            raise WireProtocolError("这个 session 已经不在运行", code="not_found")
        return entry.url

    def session_credential(self, scope: Scope) -> str:
        """biz 自己调 agent 时要用的那一个凭证。session 不在了就是调不通。"""
        entry = self._sessions.get(scope)
        if entry is None:
            raise WireProtocolError("这个 session 已经不在运行", code="not_found")
        return entry.credential

    def authenticate(self, credential: str) -> SessionIdentity:
        """session 级凭证换身份。

        **比对走常数时间**，且这个方法只认自己签过的 —— 凭证不进配置文件，只有这张表
        里有。表里没有意味着这个 session 已经被回收，凭证随之作废。
        """
        with self._lock:
            entries = tuple(self._sessions.values())
        for entry in entries:
            if secrets.compare_digest(entry.credential, credential):
                return SessionIdentity(entry.scope.user_id, entry.scope.session_id)
        raise PermissionDeniedError("Unknown credential")

    def sessions(self) -> tuple[tuple[str, str, int], ...]:
        with self._lock:
            return tuple(
                (e.scope.user_id, e.scope.session_id, e.connections)
                for e in self._sessions.values()
            )

    # -- 生命周期 ----------------------------------------------------------

    def start(self) -> None:
        self._reaper = threading.Thread(target=self._reap, name="oath-manager-reap", daemon=True)
        self._reaper.start()

    def close(self) -> None:
        self._stop.set()
        if self._reaper is not None:
            self._reaper.join(timeout=2)
        with self._lock:
            entries = tuple(self._sessions.values())
            self._sessions.clear()
        for entry in entries:
            self._stop_entry(entry)

    # -- 拉起 --------------------------------------------------------------

    def _allocate_port(self) -> int:
        start, end = self._config.manager.port_range_start, self._config.manager.port_range_end
        for _ in range(_PORT_TRIES):
            port = start + secrets.randbelow(end - start + 1)
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    continue
            return port
        raise RuntimeError(f"{start}-{end} 里找不到可用端口")

    def _launch(self, entry: SessionEntry, *, wait: bool = True) -> None:
        """新建一个 scope 的两个进程。只在 `create_session` 上用。"""
        self._start_agent(entry, wait=wait)
        self._start_supervisor(entry)

    def _start_agent(self, entry: SessionEntry, *, wait: bool = False) -> None:
        """拉起 agent。

        **`wait` 只在 `create_session` 那条路上开** —— 返回的 ws_url 必须当场连得上。
        """
        entry.agent = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "bootstrap.agent.process",
                "--config",
                self._config_path,
                "--user-id",
                entry.scope.user_id,
                "--session-id",
                entry.scope.session_id,
                "--port",
                str(entry.port),
            ],
            stdin=subprocess.DEVNULL,
            env={**os.environ, SESSION_CREDENTIAL_ENV: entry.credential},
        )
        if wait:
            self._await_listening(entry)

    def _start_supervisor(self, entry: SessionEntry) -> None:
        """拉起 supervisor。agent 不参与这件事。

        换之前先停掉旧的：**不换出一个没人管的 supervisor**，它会一直往 system 队列里投
        强制唤醒，把这个已经没人用的 scope 叫醒。
        """
        self._stop_supervisor(entry)
        supervisor = self._context.Process(
            target=_supervise,
            args=(
                self._config_path,
                entry.scope.user_id,
                entry.scope.session_id,
            ),
            name=f"oath-supervisor-{entry.scope.session_id}",
            daemon=True,
        )
        supervisor.start()
        entry.supervisor = supervisor

    def _await_listening(self, entry: SessionEntry) -> None:
        """等 agent 真的接受连接。

        `create_session` 返回的 ws_url 必须当场连得上 —— 否则每一个调用方都得自己写一遍
        「连不上就重试」，而重试多久、算不算失败，各家会答得不一样。
        agent 中途退出立刻报错：等满一个超时只会把一个已经确定的失败拖成慢的失败。
        """
        deadline = time.monotonic() + self._config.manager.launch_timeout_seconds
        host, _, port = entry.url.rpartition("//")[2].partition(":")
        while time.monotonic() < deadline:
            if entry.agent is not None and entry.agent.poll() is not None:
                raise RuntimeError(
                    f"agent 起不来（退出码 {entry.agent.returncode}），看 data 下的日志"
                )
            with socket.socket() as probe:
                probe.settimeout(0.2)
                if probe.connect_ex((host, int(port))) == 0:
                    return
            time.sleep(0.05)
        raise RuntimeError(
            f"agent 在 {self._config.manager.launch_timeout_seconds}s 内没有接受连接"
        )

    def _stop_supervisor(self, entry: SessionEntry) -> None:
        """停掉 supervisor。"""
        if entry.supervisor is not None:
            entry.supervisor.terminate()
            entry.supervisor.join(timeout=self._config.supervisor.shutdown_timeout_seconds)
            if entry.supervisor.is_alive():
                entry.supervisor.kill()
                entry.supervisor.join(timeout=2)
            entry.supervisor = None

    def _stop_entry(self, entry: SessionEntry) -> None:
        self._stop_supervisor(entry)
        if entry.agent is not None:
            if entry.agent.poll() is None:
                entry.agent.terminate()
                try:
                    entry.agent.wait(timeout=self._config.supervisor.shutdown_timeout_seconds)
                except subprocess.TimeoutExpired:
                    entry.agent.kill()
                    entry.agent.wait(timeout=2)
            entry.agent = None

    # -- 回收 --------------------------------------------------------------

    def _reap(self) -> None:
        """一轮：换掉死掉的 supervisor、看每个 agent 的连接数、必要时回收。"""
        while not self._stop.wait(self._config.manager.reap_interval_seconds):
            try:
                self._reap_round()
            except Exception:
                _log.exception("manager 一轮失败")

    def _reap_round(self) -> None:
        now = time.monotonic()
        grace = self._config.manager.idle_grace_seconds
        with self._lock:
            entries = tuple(self._sessions.values())
        for entry in entries:
            if entry.supervisor is not None and not entry.supervisor.is_alive():
                # supervisor 自己没了，换一个。agent 不动 —— 它是 agent 唯一的主人。
                _log.warning("supervisor 退出，重换一个 %s", entry.scope.session_id)
                try:
                    self._start_supervisor(entry)
                except Exception:
                    _log.exception("重启 supervisor 失败")
            timeout = self._config.manager.probe_timeout_seconds
            connections = _probe_connections(entry, timeout)
            if connections is None:
                # 探不到就是 agent 不在了。**没人重启它** —— supervisor 现在只管周期
                # 唤醒。这个 scope 会一直空着，直到有人重新 `create_session`。
                entry.connections = -1
                entry.idle_since = None
                continue
            entry.connections = connections
            if connections > 0:
                entry.idle_since = None
                continue
            if entry.idle_since is None:
                entry.idle_since = now
                continue
            if now - entry.idle_since < grace:
                continue
            _log.info("回收 %s：连续 %ss 没有连接", entry.scope.session_id, grace)
            with self._lock:
                self._sessions.pop(entry.scope, None)
            self._stop_entry(entry)


def _supervise(config_path: str, user_id: str, session_id: str) -> None:
    """supervisor 子进程的入口。真正干活的是 `bootstrap.supervisor.watchdog`。"""
    from bootstrap.supervisor.watchdog import supervise

    supervise(config_path, user_id, session_id)


def _probe_connections(entry: SessionEntry, timeout: float) -> int | None:
    """问 agent 现在有几条连接。探不到返回 `None` —— 那与「0 条」是两件事。"""
    from websockets.sync.client import connect

    try:
        with connect(
            entry.url + "/heartbeat",
            open_timeout=timeout,
            close_timeout=min(timeout, 0.2),
        ) as connection:
            reply = json.loads(connection.recv(timeout=timeout))
    except Exception:
        return None
    payload = reply.get("payload") if isinstance(reply, dict) else None
    if not isinstance(payload, dict) or payload.get("status") != "alive":
        return None
    connections = payload.get("connections")
    return connections if isinstance(connections, int) else None


__all__ = ["SessionEntry", "SessionLimitReached", "SessionManager"]
