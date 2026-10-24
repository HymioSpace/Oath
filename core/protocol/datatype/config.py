"""配置的数据类型。

一份配置管三块：模型、server、路径。全部 frozen dataclass，由 `bootstrap` 从
`oath.toml` 读出来（读法是装配层的事，core 只声明形状）。

模型凭据用 Vault 别名引用；本地 oath.toml 不进 Git。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from core.utils.encoding import JsonObject


@dataclass(frozen=True, slots=True)
class ModelProviderConfig:
    """模型服务的接入点。

    `extra_params` 透传给模型 API 的请求体：不同厂家那些名字各异的参数走它，
    core 不为任何一个厂家开字段。

    三个字段都有空值默认：配置读不全时先装得起来，报错发生在真的去调模型的那一步
    （`api_base` 为空、api key 取不到），而不是启动时一句看不懂的构造器签名错。
    """

    model: str = ""
    api_base: str = ""
    credential: str = ""
    import_env: str = ""
    timeout_seconds: float = 60.0
    extra_params: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """agent 跑一轮要用的参数。"""

    system_prompt: str = ""
    system_prompt_file: Path | None = None
    context_provider_url: str = ""
    context_provider_token_file: str = ""
    provider: ModelProviderConfig = field(default_factory=ModelProviderConfig)
    selectable_models: tuple[str, ...] = ()
    model_providers: dict[str, ModelProviderConfig] = field(default_factory=dict)
    max_output_tokens: int = 2048
    temperature: float | None = None

    #: 上下文窗口的 token 数。用来判断该不该压缩。
    context_window: int = 256_000
    model_context_windows: dict[str, int] = field(default_factory=dict)
    #: 单条工具结果超过这个字符数就保头留尾，全文存进 detail store。
    tool_result_detail_chars: int = 4000
    #: 用量到窗口的这个比例就在后台备一份压缩产物，不生效。
    compress_async_at_ratio: float = 0.4
    #: 到这个比例就必须把 context 压到能发。同一个请求只压一次。
    compress_sync_at_ratio: float = 0.9
    #: 压缩保留末尾这么多条消息。
    compress_keep_recent: int = 12
    #: 压缩结果的规模上限，也是压缩调用的输出上限。
    compress_summary_tokens: int = 1024

    #: 内置扩展根目录。每个目录下扫描 `tools/`、`cli/`、`skills/`。
    #: 外部扩展由 `tool_dirs`、`cli_dirs`、`skill_dirs` 分别指定。
    extension_dirs: tuple[Path, ...] = (Path("./extensions"),)
    tool_dirs: tuple[Path, ...] = ()
    cli_dirs: tuple[Path, ...] = ()
    skill_dirs: tuple[Path, ...] = ()
    #: 项目自己的技能目录。进 system prompt 末尾的技能索引。
    skill_dir: Path = Path("./extensions/skills")
    #: 技能索引进 system prompt 的 token 预算。装不下就按优先级丢。
    skill_index_tokens: int = 1024
    #: 额外扫到的技能目录。它们只能被 skill 工具搜到，不进 system prompt。
    skill_scan_dirs: tuple[Path, ...] = ()

    #: 工作区根目录。bash 的默认 cwd，进 system prompt 的工作区段落。
    workspace_root: Path = Path("./workspace")
    builtin_bash: bool = True
    #: 挂 `oath-cli` 的目录，前置到 bash 子进程的 PATH 上。
    cli_bin_dir: Path = Path("./scripts")

    #: 一次 run 的推理预算，工具返回触发的新 step 也计入。
    max_run_steps: int = 32
    max_run_seconds: float = 3600
    max_tool_only_steps: int = 100
    llm_max_attempts: int = 3
    llm_retry_delay: float = 0.2
    #: 派给工具系统的单次执行超时。
    tool_timeout_seconds: float = 30.0
    #: 一轮里的工具结果等多久就不等了。**从第一个结果到达起算**，不是从派发起算。
    #: 固定窗口到期可唤醒；缺失响应在 step.input 固定为 reminder。
    tool_round_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.selectable_models, (list, tuple)) or any(
            not isinstance(m, str) or not m.strip() or len(m) > 128 for m in self.selectable_models
        ):
            raise ValueError("selectable_models must contain model identifiers")
        object.__setattr__(self, "selectable_models", tuple(self.selectable_models))
        if any(
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 128
            or not isinstance(provider, ModelProviderConfig)
            or not provider.model
            or not provider.api_base
            or not provider.credential
            for name, provider in self.model_providers.items()
        ):
            raise ValueError("model_providers requires named, configured providers")
        if self.provider.model in self.model_providers:
            raise ValueError("model_providers must not replace the default provider")
        from core.context.policy import validate_windows

        validate_windows(self.model_context_windows)
        if not isinstance(self.builtin_bash, bool):
            raise ValueError("builtin_bash must be boolean")
        for name in (
            "max_run_steps",
            "max_tool_only_steps",
            "max_run_seconds",
            "llm_max_attempts",
            "llm_retry_delay",
            "tool_timeout_seconds",
            "tool_round_timeout_seconds",
            "context_window",
            "tool_result_detail_chars",
            "compress_keep_recent",
            "compress_summary_tokens",
            "skill_index_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须是有限正数")
        for name in (
            "max_run_steps",
            "max_tool_only_steps",
            "llm_max_attempts",
            "context_window",
            "tool_result_detail_chars",
            "compress_keep_recent",
            "compress_summary_tokens",
            "skill_index_tokens",
        ):
            if not isinstance(getattr(self, name), int):
                raise ValueError(f"{name} 必须是正整数")
        for name in ("compress_async_at_ratio", "compress_sync_at_ratio"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError(f"{name} 必须落在 (0, 1]")
        if self.compress_sync_at_ratio <= self.compress_async_at_ratio:
            raise ValueError("同步阈值必须高于异步阈值")


@dataclass(frozen=True, slots=True)
class ServicesConfig:
    broker_url: str = ""
    rabbitmq_user: str = "oath"
    rabbitmq_password: str = ""
    bind_host: str = "127.0.0.1"
    storage_url: str = "http://127.0.0.1:8770"
    queue_url: str = "http://127.0.0.1:8771"
    manager_url: str = "http://127.0.0.1:8772"
    tools_url: str = "http://127.0.0.1:8773"
    gateway_host: str = "127.0.0.1"
    gateway_port: int = 8765
    biz_url: str = "http://127.0.0.1:8766"
    biz_host: str = "127.0.0.1"
    biz_port: int = 8766
    internal_token: str = ""
    users: dict[str, str] = field(default_factory=dict)
    request_timeout: float = 10.0
    poll_seconds: float = 0.25
    lease_seconds: float = 60.0
    workers: int = 4
    max_connections: int = 512

    def __post_init__(self) -> None:
        for name in ("request_timeout", "poll_seconds", "lease_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.lease_seconds < self.request_timeout * 3:
            raise ValueError("lease_seconds must be at least 3 * request_timeout")
        if self.workers < 1 or self.max_connections < 1:
            raise ValueError("workers and max_connections must be positive")
        for name in ("storage_url", "queue_url", "manager_url", "tools_url"):
            url = urlsplit(getattr(self, name))
            if url.scheme not in {"http", "https"} or not url.hostname or not url.port:
                raise ValueError(f"invalid {name}")


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    invoke_interval_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.invoke_interval_seconds) or self.invoke_interval_seconds <= 0:
            raise ValueError("invoke_interval_seconds must be finite and positive")


@dataclass(frozen=True, slots=True)
class SecretsConfig:
    vault_url: str = "http://127.0.0.1:8200"
    role_id_file: Path = Path("./data/private/vault-role-id")
    secret_id_file: Path = Path("./data/private/vault-secret-id")


@dataclass(frozen=True, slots=True)
class OathConfig:
    data_root: Path = field(default_factory=lambda: Path("./data"))
    services: ServicesConfig = field(default_factory=ServicesConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)
    secrets: SecretsConfig = field(default_factory=SecretsConfig)

    @property
    def database_path(self) -> Path:
        return self.data_root / "oath.sqlite3"
