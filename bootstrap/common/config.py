"""从 `oath.toml` 读配置。

本地配置不进 Git；模型密钥只用 Vault 别名引用。
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

from core.protocol.datatype.config import (
    AgentConfig,
    ModelProviderConfig,
    OathConfig,
    SecretsConfig,
    ServicesConfig,
    SupervisorConfig,
)

#: 已知 coding agent 的技能目录。这里只记路径 —— 它们各自的文件格式不解析，共同的
#: 那部分就是 `SKILL.md`。Codex 的内置技能多一层 `.system`。
_AGENT_SKILL_DIRS: Final = (
    ".claude/skills",
    ".agents/skills",
    ".codex/skills",
    ".codex/skills/.system",
)


def _absolute(value: Any, default: Path, base: Path) -> Path:
    """目录字段在装配期变成绝对路径。

    **相对路径按 runtime 进程的工作目录算，不是按命令的工作目录。** 这两个 cwd 不是
    一回事：bash 命令默认落在 workspace 下，`cli_bin_dir` 若照原样进 `PATH`，命令就会
    去找 `workspace/scripts/oath-cli`。
    """
    path = Path(str(value)).expanduser() if value else default
    return path if path.is_absolute() else base / path


def _absolute_dirs(values: Any, default: tuple[Path, ...], base: Path) -> tuple[Path, ...]:
    """一串目录字段，同样在装配期变成绝对路径。

    配置写空数组就是关掉扫描 —— 那是一个有意义的取值，不是「没配」，所以这里靠
    `None` 与 `[]` 分开，不拿「空」当缺省。缺省值同样要绝对化，否则同一个目录会
    因写法不同而指向两条路径。
    """
    paths = (Path(str(value)).expanduser() for value in (default if values is None else values))
    return tuple(path if path.is_absolute() else base / path for path in paths)


def load_config(path: str | Path, *, profile: str = "local") -> OathConfig:
    """读一份配置。文件不存在直接报错 —— 起不来就说清楚为什么。"""
    file = Path(path)
    if not file.exists():
        raise FileNotFoundError(f"配置文件不存在: {file}")
    with file.open("rb") as handle:
        data = tomllib.load(handle)
    if profile == "docker":
        data = docker_profile(data)
    elif profile != "local":
        raise ValueError(f"Unknown deployment profile: {profile}")
    return from_dict(data, base=file.resolve().parent)


def docker_profile(data: dict[str, Any]) -> dict[str, Any]:
    """Map host paths and service addresses to the Compose network."""
    data = {
        **data,
        "services": dict(data.get("services", {})),
        "agent": dict(data.get("agent", {})),
    }
    data["data_root"] = "/data"
    data["services"].update(
        bind_host="0.0.0.0",
        storage_url="http://storage:8770",
        queue_url="http://queue:8771",
        manager_url="http://manager:8772",
        tools_url="http://tools:8773",
        biz_url="http://biz:8766",
        gateway_host="0.0.0.0",
        gateway_port=8765,
        biz_host="0.0.0.0",
        biz_port=8766,
    )
    data["agent"].update(workspace_root="/workspace", cli_bin_dir="/app/scripts")
    data["services"].update(
        broker_url="amqp://"
        + quote(data["services"].get("rabbitmq_user", "oath"), safe="")
        + ":"
        + quote(data["services"].get("rabbitmq_password", ""), safe="")
        + "@rabbitmq:5672/%2F"
    )
    data["secrets"] = {
        **data.get("secrets", {}),
        "vault_url": "http://vault:8200",
        "role_id_file": "/run/oath/role-id",
        "secret_id_file": "/run/oath/secret-id",
    }
    return data


def from_dict(data: dict[str, Any], *, base: Path | None = None) -> OathConfig:
    """TOML 的 dict → 配置对象。缺省值保证最小可启动。"""
    base = base or Path.cwd()
    unknown = set(data) - {"data_root", "agent", "services", "supervisor", "secrets"}
    if unknown:
        raise ValueError(
            f"Unsupported configuration sections: {sorted(unknown)}; use oath.example.toml"
        )
    agent_raw = data.get("agent", {})
    # provider 嵌在 `[agent.provider]` 下：它是 agent 的一个字段，不是平级的一块。
    provider_raw = agent_raw.get("provider", {})

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
            "extension_dirs",
            "skill_dir",
            "skill_scan_dirs",
            "workspace_root",
            "cli_bin_dir",
        )
    }
    defaults = AgentConfig()
    agent_fields["extension_dirs"] = _absolute_dirs(
        agent_raw.get("extension_dirs"), defaults.extension_dirs, base
    )
    # `skill_dir` 默认就是 `extension_dirs[0]/skills`，所以它也走绝对化 —— 同一个目录
    # 被两条不同写法的路径指到，只因 `SkillSystem` 按 resolve 去重才没出事。
    agent_fields["skill_dir"] = _absolute(agent_raw.get("skill_dir"), defaults.skill_dir, base)
    agent_fields["workspace_root"] = _absolute(
        agent_raw.get("workspace_root"), defaults.workspace_root, base
    )
    agent_fields["cli_bin_dir"] = _absolute(
        agent_raw.get("cli_bin_dir"), defaults.cli_bin_dir, base
    )
    agent_fields["skill_scan_dirs"] = (
        Path.cwd() / ".claude" / "skills",
        *(Path.home() / name for name in _AGENT_SKILL_DIRS),
        *(Path(path) for path in agent_raw.get("skill_scan_dirs", ())),
    )

    return OathConfig(
        data_root=_absolute(data.get("data_root"), Path("./data"), base),
        services=ServicesConfig(**data.get("services", {})),
        supervisor=SupervisorConfig(**data.get("supervisor", {})),
        secrets=SecretsConfig(
            vault_url=data.get("secrets", {}).get("vault_url", "http://127.0.0.1:8200"),
            role_id_file=_absolute(
                data.get("secrets", {}).get("role_id_file"),
                Path("./data/private/vault-role-id"), base,
            ),
            secret_id_file=_absolute(
                data.get("secrets", {}).get("secret_id_file"),
                Path("./data/private/vault-secret-id"), base,
            ),
        ),
        agent=AgentConfig(
            system_prompt=agent_raw.get("system_prompt", ""),
            provider=ModelProviderConfig(**provider_raw),
            **agent_fields,
        ),
    )


__all__ = ["from_dict", "load_config"]
