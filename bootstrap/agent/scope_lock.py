"""一个 scope 目录的排他锁。

**一个 scope 目录同时只能有一个 journal writer。** 两个 writer 往同一个目录写，
后写的会把前一个的尾部修复判断搅乱 —— 表现是随机丢记录，而且丢的时候没有任何日志。
锁是按 scope 拿的：manager 起两个 agent，它们各写各的目录，互不阻塞。

锁是 OS 级的 advisory lock：进程死了内核自动放掉，不会留下一个要人工清理的锁文件。
**拿不到就是拿不到** —— 报失败，不去猜持锁的是谁、更不去杀掉它。
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class ScopeBusy(Exception):
    """这个 scope 目录已经有人在写了。"""


class ScopeLock:
    """持有一个 scope 目录的写权限。

    `flock` 绑在 fd 上：进程退出时内核放掉，文件本身留着不动 —— 里面没有状态，
    锁就是那把 inode 上的锁。
    """

    def __init__(self, scope_root: Path) -> None:
        self._path = scope_root / "agent.lock"
        self._fd: int | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            raise ScopeBusy(f"{self._path.parent} 已经有一个 agent 在跑") from error
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        os.close(self._fd)  # 关闭即解锁，不需要显式 LOCK_UN。
        self._fd = None

    def __enter__(self) -> ScopeLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


__all__ = ["ScopeBusy", "ScopeLock"]
