"""One agent process for one scope: three consumers, one brain, one WS listener."""

from __future__ import annotations

import argparse
import asyncio
import logging

from redis import Redis

from bootstrap.agent.agent import Agent
from bootstrap.agent.endpoint import AgentEndpoint
from bootstrap.agent.scope_lock import ScopeLock
from bootstrap.agent.server import serve
from bootstrap.common.authentication import read_session_credential
from bootstrap.common.config import load_config
from bootstrap.common.lifecycle import until_stopped
from core.protocol.datatype.scope import Scope, is_valid_scope_id

_log = logging.getLogger("oath.agent")


async def run(config_path: str, user_id: str, session_id: str, port: int) -> None:
    config = load_config(config_path)
    scope = Scope(user_id, session_id)
    credential = read_session_credential()
    if not credential:
        raise SystemExit("缺少 session 凭证环境变量，没有凭证就不开这个 session")
    loop = asyncio.get_running_loop()
    redis = Redis.from_url(
        config.middleware.redis_url,
        socket_timeout=config.middleware.redis_timeout_seconds,
        socket_connect_timeout=config.middleware.redis_timeout_seconds,
        decode_responses=False,
    )
    with ScopeLock(config.data_root / scope.user_id / scope.session_id):
        agent = Agent(config, scope, loop=loop, credential=credential, redis=redis)
        endpoint = AgentEndpoint(agent, agent.authenticator, timeout=5.0)
        # 先起消费线程再监听。**反过来就有一个真空期**：队列里的输入已经进了 Redis
        # Streams，但这个进程还没有人收它，而 manager 的存活探测还探得到这个端口。
        agent.start()
        try:
            await until_stopped(
                serve(
                    agent,
                    host=config.server.host,
                    port=port,
                    max_connections=config.server.max_connections,
                    service_handler=endpoint.serve,
                )
            )
        finally:
            agent.hub.shutdown()
            agent.close()


def main(config_path: str, user_id: str, session_id: str, port: int) -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(config_path, user_id, session_id, port))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Oath agent（一个 scope 一个进程）")
    parser.add_argument("--config", default="oath.toml")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    if not is_valid_scope_id(args.user_id) or not is_valid_scope_id(args.session_id):
        raise SystemExit("user_id / session_id 不合法")
    main(args.config, args.user_id, args.session_id, args.port)
