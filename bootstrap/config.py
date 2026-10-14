"""从 `oath.toml` 读配置。

字符串里的 `${VAR}` 会展开成环境变量。**凭证走这条**：配置文件里只写
`api_key_env = "OPENAI_API_KEY"`，值从环境里取 —— 配置文件进 git，写死 key 等于
公开它。
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any, Final

from core.protocol.datatype.config import (
    AgentConfig,
    ModelProviderConfig,
    OathConfig,
    ServerConfig,
)

_ENV_RE: Final = re.compile(r"\$\{([^}]+)\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_config(path: str | Path) -> OathConfig:
    """读一份配置。文件不存在直接报错 —— 起不来就说清楚为什么。"""
    file = Path(path)
    if not file.exists():
        raise FileNotFoundError(f"配置文件不存在: {file}")
    with file.open("rb") as handle:
        return from_dict(_expand(tomllib.load(handle)))


def from_dict(data: dict[str, Any]) -> OathConfig:
    """TOML 的 dict → 配置对象。缺省值保证最小可启动。"""
    agent_raw = data.get("agent", {})
    server_raw = data.get("server", {})
    # provider 嵌在 `[agent.provider]` 下：它是 agent 的一个字段，不是平级的一块。
    provider_raw = agent_raw.get("provider", {})

    credentials: dict[str, tuple[str, str]] = {}
    for entry in data.get("credentials", []):
        token = entry.get("token")
        if not token:
            continue
        credentials[token] = (
            str(entry.get("user_id", "")),
            str(entry.get("session_id", "")),
        )

    return OathConfig(
        data_root=Path(data.get("data_root", "./data")),
        server=ServerConfig(**server_raw),
        agent=AgentConfig(
            system_prompt=agent_raw.get("system_prompt", ""),
            provider=ModelProviderConfig(**provider_raw),
            **{k: v for k, v in agent_raw.items() if k not in ("system_prompt", "provider")},
        ),
        credentials=credentials,
    )


__all__ = ["from_dict", "load_config"]
