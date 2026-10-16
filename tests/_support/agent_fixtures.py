"""单 scope 的 `Agent` 装配：模型走脚本，Redis 走进程内替身。

**这里验的是 Agent 装配之后的行为**，不是队列中间件 —— 真 Redis 那份有单独的 e2e。
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any

import fakeredis
import pytest

from bootstrap.agent.agent import Agent
from core.protocol.datatype.config import AgentConfig, OathConfig
from core.protocol.datatype.scope import Scope
from core.protocol.interface.llm import ModelProvider
from core.protocol.interface.permission import PermissionPolicy
from core.protocol.interface.tool import Tool
from tests._support.intake_fixtures import ScriptedProvider, ScriptedResponse

SCOPE = Scope("u1", "s1")
CREDENTIAL = "session-credential-for-tests"


def agent_config(tmp_path: Path, **agent: Any) -> OathConfig:
    """测试用的配置：默认没有扩展目录，技能目录与工作区都指到 tmp_path 之下。

    传进来的键覆盖默认值。
    """
    defaults: dict[str, Any] = {
        "extension_dirs": (),
        "skill_dir": tmp_path / "skills",
        "skill_scan_dirs": (),
        "workspace_root": tmp_path / "workspace",
        "cli_bin_dir": tmp_path / "scripts",
    }
    return OathConfig(data_root=tmp_path, agent=AgentConfig(**{**defaults, **agent}))


def build(
    tmp_path: Path,
    *,
    provider: ModelProvider | None = None,
    responses: tuple[ScriptedResponse, ...] = (),
    tools: list[Tool] | None = None,
    policy: PermissionPolicy | None = None,
    scope: Scope = SCOPE,
    loop: asyncio.AbstractEventLoop | None = None,
    config: OathConfig | None = None,
    **agent: Any,
) -> Agent:
    """装配一个 Agent，**不起消费线程**。调用方自己决定要不要 `start()`。"""
    return Agent(
        config or agent_config(tmp_path, **agent),
        scope,
        loop=loop or asyncio.new_event_loop(),
        credential=CREDENTIAL,
        redis=fakeredis.FakeRedis(),
        tools=tools,
        policy=policy,
        provider=provider or ScriptedProvider(responses),
    )


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    instance = asyncio.new_event_loop()
    yield instance
    instance.close()


@pytest.fixture
def provider() -> ScriptedProvider:
    return ScriptedProvider()


@pytest.fixture
def agent(
    tmp_path: Path, loop: asyncio.AbstractEventLoop, provider: ScriptedProvider
) -> Generator[Agent, None, None]:
    """已经起了消费线程的 Agent。"""
    instance = build(tmp_path, provider=provider, loop=loop)
    instance.start()
    yield instance
    instance.close()


__all__ = ["CREDENTIAL", "SCOPE", "agent_config", "build"]
