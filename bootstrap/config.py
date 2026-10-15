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

#: 已知 coding agent 的技能目录。这里只记路径 —— 它们各自的文件格式不解析，共同的
#: 那部分就是 `SKILL.md`。Codex 的内置技能多一层 `.system`。
_AGENT_SKILL_DIRS: Final = (
    ".claude/skills",
    ".agents/skills",
    ".codex/skills",
    ".codex/skills/.system",
)


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _absolute(value: Any, default: Path) -> Path:
    """目录字段在装配期变成绝对路径。

    **相对路径按 runtime 进程的工作目录算，不是按命令的工作目录。** 这两个 cwd 不是
    一回事：bash 命令默认落在 workspace 下，`cli_bin_dir` 若照原样进 `PATH`，命令就会
    去找 `workspace/scripts/oath-cli`。
    """
    path = Path(str(value)).expanduser() if value else default
    return path if path.is_absolute() else Path.cwd() / path


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

    # 目录类的字段在 TOML 里是字符串，读进来就得转成 Path / tuple —— 声明的形状
    # 只有这一处保证。coding agent 的技能目录在这里展开成绝对路径：程序自己拼的
    # `~` 只有装配期知道，core 不读 home。
    agent_fields = {
        k: v
        for k, v in agent_raw.items()
        if k
        not in (
            "system_prompt",
            "provider",
            "skill_dir",
            "skill_scan_dirs",
            "workspace_root",
            "cli_bin_dir",
        )
    }
    defaults = AgentConfig()
    agent_fields["skill_dir"] = Path(agent_raw.get("skill_dir", defaults.skill_dir))
    agent_fields["workspace_root"] = _absolute(
        agent_raw.get("workspace_root"), defaults.workspace_root
    )
    agent_fields["cli_bin_dir"] = _absolute(agent_raw.get("cli_bin_dir"), defaults.cli_bin_dir)
    agent_fields["skill_scan_dirs"] = (
        Path.cwd() / ".claude" / "skills",
        *(Path.home() / name for name in _AGENT_SKILL_DIRS),
        *(Path(path) for path in agent_raw.get("skill_scan_dirs", ())),
    )

    return OathConfig(
        data_root=Path(data.get("data_root", "./data")),
        server=ServerConfig(**server_raw),
        agent=AgentConfig(
            system_prompt=agent_raw.get("system_prompt", ""),
            provider=ModelProviderConfig(**provider_raw),
            **agent_fields,
        ),
        credentials=credentials,
    )


__all__ = ["from_dict", "load_config"]
