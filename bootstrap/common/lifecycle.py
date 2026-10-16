"""Signal-driven lifecycle for standalone async services."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Coroutine
from typing import Any


async def until_stopped(server: Coroutine[Any, Any, None]) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    task = asyncio.create_task(server)
    stopped = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait((task, stopped), return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
    finally:
        stopped.cancel()
        task.cancel()
        await asyncio.gather(task, stopped, return_exceptions=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
