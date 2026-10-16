"""Standalone biz service: session manager, HTTP routes, CLI execution."""

from __future__ import annotations

import argparse
import asyncio
import logging

from bootstrap.biz.assembly import build_application
from bootstrap.biz.server import serve
from bootstrap.common.config import load_config
from bootstrap.common.lifecycle import until_stopped
from bootstrap.manager import SessionManager

_log = logging.getLogger("oath.biz")


async def run(config_path: str) -> None:
    config = load_config(config_path)
    manager = SessionManager(config, config_path)
    app = build_application(config, manager)
    manager.start()
    try:
        await until_stopped(
            serve(app, manager, config, host=config.server.host, port=config.server.http_port)
        )
    finally:
        # 先停 manager 再停 server：server 在收尾时还可能接得住一个 `create_session`，
        # 那时 manager 已经没人拉起进程了。
        manager.close()


def main(config_path: str) -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(config_path))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Oath biz service")
    parser.add_argument("--config", default="oath.toml")
    main(parser.parse_args().config)
