"""每个 scope 的 supervisor：站在 agent 外面，按固定间隔投一次强制唤醒。

干预只有一种形式：往 system 队列投事件。**目前落地的能力只有周期唤醒这一项** —— 它不看
agent 的存活，也不发输入、不读 journal、不看连接数。

队列投不出去就是中间件不可用 —— 异常抛出去，`Periodic` 会退避，不假装投成功了。
"""

from __future__ import annotations

import logging
import signal
import threading
import time

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
    """一个 scope 的周期唤醒器。"""

    def __init__(
        self,
        policy: SupervisorConfig,
        queue: RedisEventQueue,
        scope: Scope,
    ) -> None:
        self._policy, self._queue = policy, queue
        self._scope = scope
        self._task = Periodic(
            "invoke",
            policy.invoke_interval_seconds,
            policy.invoke_timeout_seconds,
            self._invoke,
        )

    def start(self) -> None:
        self._task.start()

    def stop(self) -> None:
        self._task.stop()

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


def supervise(config_path: str, user_id: str, session_id: str) -> None:
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
    supervisor = ScopeSupervisor(config.supervisor, RedisEventQueue(redis), scope)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        supervisor.start()
        stop.wait()
    finally:
        supervisor.stop()


__all__ = ["ScopeSupervisor", "supervise"]
