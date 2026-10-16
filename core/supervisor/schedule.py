"""Supervisor 的周期工作。

每项任务最多一个在途轮次，慢的一轮不会堆叠也不会拖住别的任务。用 monotonic 时间：
墙上时钟回拨只会让一轮晚点开始，不会让它提前。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

_log = logging.getLogger(__name__)

#: 连续失败后的退避上限（秒）。再往后不继续翻倍 —— 退避到几分钟就没有意义了。
_BACKOFF_CAP: Final = 60.0


@dataclass
class Periodic:
    """一个周期任务。

    `work` 收到的是本轮的 monotonic 截止时间，自己负责在超时前收手。所有 I/O 都有
    timeout —— supervisor 自己卡住等于没人看着 runtime 了。
    """

    name: str
    interval: float
    timeout: float
    work: Callable[[float], None]
    backoff_base: float = 2.0

    def __post_init__(self) -> None:
        self._stop = threading.Event()
        self._running = threading.Event()
        self._failures = 0
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name=f"oath-sup-{self.name}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _delay(self) -> float:
        if not self._failures:
            return self.interval
        return min(self.interval * (self.backoff_base ** min(self._failures, 30)), _BACKOFF_CAP)

    def _loop(self) -> None:
        while not self._stop.wait(self._delay()):
            if self._running.is_set():
                _log.warning("%s 上一轮还在跑，跳过本轮", self.name)
                continue
            self._run_once()

    def _run_once(self) -> None:
        self._running.set()
        thread = threading.Thread(target=self._work, name=f"oath-sup-{self.name}-run", daemon=True)
        thread.start()
        thread.join(timeout=self.timeout + self.interval)
        if thread.is_alive():
            # 超时的轮次自己跑完，不能中断 —— 它的线程是 daemon，进程退出时自然收掉。
            # 在途标记由它自己收尾清；这里清了下一轮就会再起一个，两轮同时跑就不再是
            # 「每项最多一个在途轮次」。
            _log.error("%s 一轮超过 %ss 仍未结束", self.name, self.timeout + self.interval)

    def _work(self) -> None:
        try:
            self.work(time.monotonic() + self.timeout)
        except Exception:
            self._failures += 1
            _log.exception("%s 本轮失败，第 %s 次", self.name, self._failures)
        else:
            self._failures = 0
        finally:
            self._running.clear()


__all__ = ["Periodic"]
