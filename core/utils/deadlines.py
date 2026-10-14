"""持久化绝对截止 + 进程内 monotonic 等待。重启回拨最多等一个配置窗口。"""

from __future__ import annotations

import threading
import time
from collections.abc import Hashable


class Deadlines:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._targets: dict[Hashable, tuple[float, float]] = {}

    def remaining(self, key: Hashable, absolute: float, window: float) -> float:
        with self._lock:
            saved = self._targets.get(key)
            if saved is None or saved[0] != absolute:
                target = time.monotonic() + max(0, min(window, absolute - time.time()))
                self._targets[key] = (absolute, target)
            return max(0, self._targets[key][1] - time.monotonic())

    def forget(self, key: Hashable) -> None:
        with self._lock:
            self._targets.pop(key, None)
