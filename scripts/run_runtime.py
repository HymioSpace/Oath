#!/usr/bin/env python3
"""起 Oath runtime：读配置、装配、监听一条 WebSocket。

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
from bootstrap.ws import serve
from extensions.tools import demo_tools


async def _main(config_path: Path) -> None:
    config = load_config(config_path)
    loop = asyncio.get_running_loop()
    runtime = Runtime(config, loop=loop, tools=demo_tools())

    stop: asyncio.Future[None] = asyncio.Future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _stop, stop)

    serving = asyncio.create_task(
        serve(
            runtime,
            host=config.server.host,
            port=config.server.port,
            max_connections=config.server.max_connections,
        )
    )
    try:
        await stop
    finally:
        serving.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serving
        runtime.close()


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
