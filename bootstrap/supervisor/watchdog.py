"""每个 scope 的 supervisor：看住自己的 agent，定时投一次强制唤醒。

**它和 agent 的推理没有任何关系。** 它只做两件事：往 system 队列投 `system.invoke`
（强制唤醒，Brain 被叫起来看一眼现有输入，这条事件本身不进 buffer），以及探测自己的
agent 是不是还在 —— 不在了就请 manager 重启。它不发输入、不读 journal、不看连接数。
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from multiprocessing.connection import Connection

from redis import Redis

from bootstrap.common.config import load_config
from core.protocol.datatype.config import SupervisorConfig
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import QueueName
from core.protocol.datatype.scope import Scope
from core.queue.redis_queue import RedisEventQueue
from core.supervisor import Periodic
from core.utils.encoding import utc_now_iso

_log = logging.getLogger("oath.supervisor")


class ScopeSupervisor:
    def __init__(
        self,
        policy: SupervisorConfig,
        queue: RedisEventQueue,
        scope: Scope,
        agent_url: str,
        control: Connection,
    ) -> None:
        self._policy, self._queue = policy, queue
        self._scope, self._url, self._control = scope, agent_url.rstrip("/"), control
        self._failures = 0
        self._restarts = 0
        self._retry_at: float | None = None
        self._tasks = (
            Periodic(
                "invoke",
                policy.invoke_interval_seconds,
                policy.heartbeat_timeout_seconds,
                self._invoke,
            ),
            Periodic(
                "heartbeat",
                policy.heartbeat_interval_seconds,
                policy.heartbeat_timeout_seconds,
                self._check,
            ),
        )

    def start(self) -> None:
        for task in self._tasks:
            task.start()

    def stop(self) -> None:
        for task in self._tasks:
            task.stop()

    # -- 强制唤醒 ----------------------------------------------------------

    def _invoke(self, deadline: float) -> None:
        """往 system 队列投一条 `system.invoke`。

        **这是一个事件，不是一次调用。** 收下它的 consumer 不把它进 buffer，Brain 只是
        被叫醒一趟。投不出去就是队列不可用 —— 让异常抛出去，`Periodic` 会退避，
        不假装投成功了。
        """
        self._queue.add(
            self._scope,
            Inbound(
                kind=QueueName.SYSTEM,
                source="system.invoke",
                source_id=f"invoke:{time.monotonic_ns()}",
                payload={},
                occur_dt=utc_now_iso(),
            ),
        )

    # -- 看住 agent --------------------------------------------------------

    def _check(self, deadline: float) -> None:
        if _alive(self._url, self._policy.heartbeat_timeout_seconds):
            self._failures = 0
            self._retry_at = None
            return
        self._failures += 1
        if self._failures < self._policy.heartbeat_failures_before_restart:
            return
        now = time.monotonic()
        if self._retry_at is None:
            self._retry_at = now + self._backoff()
        if now < self._retry_at:
            return
        self._restarts += 1
        try:
            self._control.send(
                {
                    "action": "restart_agent",
                    "user_id": self._scope.user_id,
                    "session_id": self._scope.session_id,
                }
            )
        except (OSError, EOFError):
            _log.exception("重启请求发不出去；manager 收不到就没有人拉起 agent")
            return
        self._failures = 0
        self._retry_at = None

    def _backoff(self) -> float:
        base = self._policy.restart_backoff_seconds
        return min(base * 2.0 ** min(self._restarts, 30), self._policy.restart_backoff_cap_seconds)


def _alive(url: str, timeout: float) -> bool:
    """心跳连接要真的走过 server 的处理循环 —— 旁路线程报活着证明不了 accept 还通。"""
    from websockets.sync.client import connect

    try:
        with connect(
            url + "/heartbeat", open_timeout=timeout, close_timeout=min(timeout, 0.2)
        ) as connection:
            reply = json.loads(connection.recv(timeout=timeout))
    except Exception:
        return False
    payload = reply.get("payload") if isinstance(reply, dict) else None
    return isinstance(payload, dict) and payload.get("status") == "alive"


def supervise(
    config_path: str, user_id: str, session_id: str, port: int, control: Connection
) -> None:
    """supervisor 子进程的入口。"""
    logging.basicConfig(level=logging.INFO)
    config = load_config(config_path)
    scope = Scope(user_id, session_id)
    redis = Redis.from_url(
        config.middleware.redis_url,
        socket_timeout=config.middleware.redis_timeout_seconds,
        socket_connect_timeout=config.middleware.redis_timeout_seconds,
        decode_responses=False,
    )
    host = "127.0.0.1" if config.server.host in {"", "0.0.0.0"} else config.server.host
    supervisor = ScopeSupervisor(
        config.supervisor,
        RedisEventQueue(redis),
        scope,
        f"ws://{host}:{port}",
        control,
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        supervisor.start()
        stop.wait()
    finally:
        supervisor.stop()
        control.close()


__all__ = ["ScopeSupervisor", "supervise"]
