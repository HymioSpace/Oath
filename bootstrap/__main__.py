"""启动一个服务；进程编排由 Docker Compose 或部署平台负责。"""

import argparse
import importlib
import logging

from bootstrap.common.config import load_config

SERVICES = {
    name: f"bootstrap.{name}.process"
    for name in ("storage", "queue", "manager", "tools", "consumer", "worker", "gateway", "biz")
}
SERVICES["supervisor"] = "bootstrap.supervisor.watchdog"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=SERVICES)
    parser.add_argument("--config", default="oath.toml")
    parser.add_argument("--profile", choices=("local", "docker"), default="local")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("pika").setLevel(logging.WARNING)
    config = load_config(args.config, profile=args.profile)
    importlib.import_module(SERVICES[args.service]).run(config)


if __name__ == "__main__":
    main()
