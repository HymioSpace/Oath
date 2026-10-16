#!/usr/bin/env python3
"""启停脚本要用的两件事：把配置读成 shell 变量，探一个 TCP 端口有没有人在听。

    oath-env.py show <config>          打印 KEY=VALUE
    oath-env.py listening <host> <port>   有人在听就退 0
    oath-env.py wait <host> <port> <秒>   听上了就退 0，超时退 1

配置由本仓库的加载器读，启停脚本自己不解析 TOML —— 两边读出来的必须是同一份。
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bootstrap.common.config import load_config


def show(path: str) -> int:
    config = load_config(path)
    url = urlparse(config.middleware.redis_url)
    print(f"REDIS_URL={config.middleware.redis_url}")
    print(f"REDIS_PORT={url.port or 6379}")
    print(f"DATA_ROOT={Path(config.data_root).resolve()}")
    print(f"HTTP_PORT={config.server.http_port}")
    return 0


def listening(host: str, port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


def wait(host: str, port: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if listening(host, port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    command = argv[1]
    if command == "show":
        # 配置写错时启停脚本只会看到退出码。给一句能照着改的话，别丢一个 traceback。
        try:
            return show(argv[2])
        except Exception as exc:
            print(f"{argv[2]} 读不出来：{exc}", file=sys.stderr)
            return 1
    if command == "listening":
        return 0 if listening(argv[2], int(argv[3])) else 1
    if command == "wait":
        return 0 if wait(argv[2], int(argv[3]), float(argv[4])) else 1
    print(f"不认识的动作：{command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
