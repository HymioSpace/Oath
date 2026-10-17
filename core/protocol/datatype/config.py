"""配置的数据类型。

一份配置管三块：模型、server、路径。全部 frozen dataclass，由 `bootstrap` 从
`oath.toml` 读出来（读法是装配层的事，core 只声明形状）。

**凭证只走环境变量。** 配置里只有 `api_key_env`（环境变量名），没有 `api_key` ——
配置文件会进 git，写死 key 等于公开它。
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
    api_key_env: str = ""
    timeout_seconds: float = 60.0
    extra_params: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """agent 跑一轮要用的参数。"""

    system_prompt: str = ""
    provider: ModelProviderConfig = field(default_factory=ModelProviderConfig)
    max_output_tokens: int = 2048
    temperature: float | None = None

    #: 上下文窗口的 token 数。用来判断该不该压缩。
    context_window: int = 128_000
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

    #: 扩展目录。每个目录下按同一套规则扫 `tools/`、`cli/`、`skills/`。
    #: 第二个是开发者本地的扩展，不进 git；没有它不是错误。工具或命令撞名会拒绝启动，
    #: 顺序只决定报错时谁是先到的那一个。
    extension_dirs: tuple[Path, ...] = (Path("./extensions"), Path("./extensions-local"))
    #: 项目自己的技能目录。进 system prompt 末尾的技能索引。
    skill_dir: Path = Path("./extensions/skills")
    #: 技能索引进 system prompt 的 token 预算。装不下就按优先级丢。
    skill_index_tokens: int = 1024
    #: 额外扫到的技能目录。它们只能被 skill 工具搜到，不进 system prompt。
    skill_scan_dirs: tuple[Path, ...] = ()

    #: 工作区根目录。bash 的默认 cwd，进 system prompt 的工作区段落。
    workspace_root: Path = Path("./workspace")
    #: 挂 `oath-cli` 的目录，前置到 bash 子进程的 PATH 上。
    cli_bin_dir: Path = Path("./scripts")

    #: 一次 run 的推理预算，工具返回触发的新 step 也计入。
    max_run_steps: int = 32
    max_run_seconds: float = 3600
    llm_max_attempts: int = 3
    llm_retry_delay: float = 0.2
    #: 派给工具系统的单次执行超时。
    tool_timeout_seconds: float = 30.0
    #: 一轮里的工具结果等多久就不等了。**从第一个结果到达起算**，不是从派发起算。
    #: 固定窗口到期可唤醒；缺失响应在 step.input 固定为 reminder。
    tool_round_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        for name in (
            "max_run_steps",
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
    broker_url_env: str = "OATH_AMQP_URL"
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
    internal_token_env: str = "OATH_SERVICE_TOKEN"
    users_env: str = "OATH_USERS"
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
class OathConfig:
    data_root: Path = field(default_factory=lambda: Path("./data"))
    services: ServicesConfig = field(default_factory=ServicesConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)

    @property
    def database_path(self) -> Path:
        return self.data_root / "oath.sqlite3"
