#!/usr/bin/env python3
"""起 Oath runtime：读配置、装配、起 WebSocket 与 biz 两个 server。

export OPENAI_API_KEY=...
uv run python scripts/run_runtime.py --config oath.toml
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bootstrap import Runtime, load_config
from bootstrap.biz_server import serve as serve_http
from bootstrap.extension_loader import ExtensionError
from bootstrap.ws_server import serve as serve_ws

_log = logging.getLogger("oath.runtime")


class StartupFailure(Exception):
    """某个 server 没起来。半启动的 runtime 一起不起还更难查，直接让进程失败。"""


async def _main(config_path: Path) -> None:
    config = load_config(config_path)
    loop = asyncio.get_running_loop()
    try:
        runtime = Runtime(config, loop=loop)
    except ExtensionError as error:
        # 扩展装不起来的消息已经说清是哪个目录、哪个文件。摊在 traceback 里等于没有 ——
        # 那条消息才是这一路要告诉使用者的东西。
        _log.error("%s", error)
        raise SystemExit(1) from None

    stop: asyncio.Future[None] = asyncio.Future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _stop, stop)

    # 两个 server 共享一个 runtime：WebSocket 收命令，HTTP 供 biz 只读查询。
    serving = {
        "WebSocket server": asyncio.create_task(
            serve_ws(
                runtime,
                host=config.server.host,
                port=config.server.port,
                max_connections=config.server.max_connections,
            )
        ),
        "biz server": asyncio.create_task(
            serve_http(runtime, host=config.server.host, port=config.server.http_port)
        ),
    }
    try:
        await _until_stopped(serving, stop)
    except StartupFailure as exc:
        # uvicorn 绑不上端口时抛的是 SystemExit，光往外抛只剩一个看不懂的退出码。
        _log.error("%s", exc)
        raise SystemExit(1) from None
    finally:
        for task in serving.values():
            task.cancel()
        # 关停时不挑异常：已经报过的那一条（比如端口起不来）不该在这里再炸一遍。
        await asyncio.gather(*serving.values(), return_exceptions=True)
        runtime.close()


async def _until_stopped(
    serving: dict[str, asyncio.Task[None]], stop: asyncio.Future[None]
) -> None:
    """等关停信号，或者任一 server 退出。

    端口被占用这类失败只发生在其中一个 task 上。没人管的话，另一端还在监听、进程
    还在等信号 —— 看着像起来了，实际只有一半能用。任一 server 退出就整体失败。
    """
    done, _pending = await asyncio.wait(
        [*serving.values(), stop], return_when=asyncio.FIRST_COMPLETED
    )
    for name, task in serving.items():
        if task in done:
            raise StartupFailure(f"{name} 没起来：{task.exception() or '意外退出'}")


def _stop(stop: asyncio.Future[None]) -> None:
    """收到信号就醒。按第二次是同一件事，不该炸。"""
    if not stop.done():
        stop.set_result(None)


def main() -> None:
    parser = argparse.ArgumentParser(description="起 Oath runtime")
    parser.add_argument("--config", default="oath.toml", help="配置文件路径")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(_main(Path(args.config)))


if __name__ == "__main__":
    main()
