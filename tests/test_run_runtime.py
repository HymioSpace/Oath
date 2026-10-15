"""进程入口：两个 server 一起活着才算起来。"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

_module = importlib.util.spec_from_file_location(
    "run_runtime", Path(__file__).resolve().parent.parent / "scripts" / "run_runtime.py"
)
assert _module and _module.loader
run_runtime = importlib.util.module_from_spec(_module)
_module.loader.exec_module(run_runtime)


async def _forever() -> None:
    await asyncio.Future()


def test_one_server_dying_takes_the_whole_process_down() -> None:
    """端口被占用这类失败只炸在其中一个 task 上。

    没人管的话，另一端照样监听、进程照样等信号 —— 看着像起来了，实际只有一半能用。
    """

    async def die() -> None:
        raise OSError("address already in use")

    async def main() -> None:
        loop = asyncio.get_running_loop()
        serving = {
            "WebSocket server": asyncio.create_task(_forever()),
            "biz server": asyncio.create_task(die()),
        }
        try:
            with pytest.raises(run_runtime.StartupFailure) as failed:
                await run_runtime._until_stopped(serving, loop.create_future())
        finally:
            for task in serving.values():
                task.cancel()
        assert "biz server 没起来" in str(failed.value)

    asyncio.run(main())


def test_a_stop_signal_is_not_a_failure() -> None:
    async def main() -> None:
        loop = asyncio.get_running_loop()
        serving = {
            "WebSocket server": asyncio.create_task(_forever()),
            "biz server": asyncio.create_task(_forever()),
        }
        stop = loop.create_future()
        stop.set_result(None)
        try:
            await run_runtime._until_stopped(serving, stop)
        finally:
            for task in serving.values():
                task.cancel()

    asyncio.run(main())
