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
from typing import Any
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
class ServerConfig:
    """监听参数。biz 的 HTTP 端口固定；每个 scope 的 agent 端口由 manager 现分配。"""

    host: str = "127.0.0.1"
    #: biz server 的端口。整个 biz 一个端口，每条查询是其中一个 path。与 agent 的
    #: WebSocket 分开：查询不是 command，不该占会话连接。
    http_port: int = 8766
    #: 每条连接的发送队列长度。满了就断开那条连接，不拖住 core。
    send_queue_size: int = 256
    #: 一条连接多久没有任何 command 就断开。
    idle_timeout_seconds: float = 300.0
    #: 一个 scope 的 agent 同时接受多少条连接。
    max_connections: int = 32


@dataclass(frozen=True, slots=True)
class MiddlewareConfig:
    """进程外中间件。queue 与 buffer 的具体实现由这两项决定。"""

    #: 队列：Redis Streams。三条队列的 key 都带 scope，中间件自己分不开也不需要分。
    redis_url: str = "redis://127.0.0.1:6379/0"
    #: 单次队列操作的超时。取不到就是取不到，不阻塞到天荒地老。
    redis_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        url = urlsplit(self.redis_url)
        if url.scheme not in {"redis", "rediss", "unix"} or not url.hostname:
            raise ValueError("redis_url must be a redis://, rediss:// or unix:// URL")
        for value, name in ((self.redis_timeout_seconds, "redis_timeout_seconds"),):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True, slots=True)
class ManagerConfig:
    """scope 的增删。manager 维护 session 表，按连接数回收。"""

    #: 同时存在多少个 scope。满了 `create_session` 直接报失败，不排队。
    max_sessions: int = 64
    #: 分配给 agent 进程的端口区间。闭区间，撞上就换一个。
    port_range_start: int = 8800
    port_range_end: int = 8899
    #: 轮询连接数的间隔。
    reap_interval_seconds: float = 5.0
    #: 单次连接数探测最多等多久。**短于间隔** —— 慢探测不能连着慢两轮。
    probe_timeout_seconds: float = 3.0
    #: 连接数连续为 0 多久之后回收这个 scope。给重连留一段冷却。
    idle_grace_seconds: float = 60.0
    #: 拉起 agent 之后等它接受连接等多久。**`create_session` 要等这个过了才返回** ——
    #: 返回一个还连不上的 ws_url 等于把重连的活推给调用方。
    launch_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        for name in ("max_sessions",):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须是正整数")
        for name in (
            "reap_interval_seconds",
            "idle_grace_seconds",
            "launch_timeout_seconds",
            "probe_timeout_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须是有限正数")
        if self.probe_timeout_seconds >= self.reap_interval_seconds:
            raise ValueError("probe_timeout_seconds 必须小于 reap_interval_seconds")
        for value, name in (
            (self.port_range_start, "port_range_start"),
            (self.port_range_end, "port_range_end"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1024 < value < 65536:
                raise ValueError(f"{name} 不是可用端口")
        if self.port_range_end < self.port_range_start:
            raise ValueError("port_range_end 不能小于 port_range_start")


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    """每个 scope 的 supervisor：定时投一次强制唤醒。"""

    #: 往 system 队列投 `system.invoke` 的间隔。**强制唤醒**：让 idle 的模型再看一眼
    #: 攒着的输入与工具结果，投递本身不进 buffer。
    invoke_interval_seconds: float = 30.0
    #: 单次投递最多等多久。**短于间隔** —— 慢投递不能连着慢两轮。
    invoke_timeout_seconds: float = 5.0
    #: 请求一个进程退出后等多久，超过了就终止它。
    shutdown_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        for name in (
            "invoke_interval_seconds",
            "invoke_timeout_seconds",
            "shutdown_timeout_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须是有限正数")
        if self.invoke_timeout_seconds >= self.invoke_interval_seconds:
            raise ValueError("invoke_timeout_seconds 必须小于 invoke_interval_seconds")


@dataclass(frozen=True, slots=True)
class OathConfig:
    """一份完整配置。"""

    data_root: Path = field(default_factory=lambda: Path("./data"))
    server: ServerConfig = field(default_factory=ServerConfig)
    middleware: MiddlewareConfig = field(default_factory=MiddlewareConfig)
    manager: ManagerConfig = field(default_factory=ManagerConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)
    #: 服务级凭证：能调 `create_session` 的那些。**不含任何 session 的凭证** ——
    #: session token 由 manager 在创建时签发，只进那一个 scope 的 agent 进程。
    service_credentials: tuple[str, ...] = ()

    def service_authorised(self, token: str) -> bool:
        return bool(token) and token in self.service_credentials

    def to_json(self) -> dict[str, Any]:
        """完整配置的 dict 形态。**不含凭证的值** —— 只有环境变量名。"""
        return {
            "data_root": str(self.data_root),
            "middleware": {
                "redis_url": self.middleware.redis_url,
                "redis_timeout_seconds": self.middleware.redis_timeout_seconds,
            },
            "manager": {
                "max_sessions": self.manager.max_sessions,
                "port_range_start": self.manager.port_range_start,
                "port_range_end": self.manager.port_range_end,
                "reap_interval_seconds": self.manager.reap_interval_seconds,
                "probe_timeout_seconds": self.manager.probe_timeout_seconds,
                "idle_grace_seconds": self.manager.idle_grace_seconds,
                "launch_timeout_seconds": self.manager.launch_timeout_seconds,
            },
            "server": {
                "host": self.server.host,
                "http_port": self.server.http_port,
                "send_queue_size": self.server.send_queue_size,
                "idle_timeout_seconds": self.server.idle_timeout_seconds,
                "max_connections": self.server.max_connections,
            },
            "agent": {
                "system_prompt": self.agent.system_prompt,
                "provider": {
                    "model": self.agent.provider.model,
                    "api_base": self.agent.provider.api_base,
                    "api_key_env": self.agent.provider.api_key_env,
                    "timeout_seconds": self.agent.provider.timeout_seconds,
                    "extra_params": dict(self.agent.provider.extra_params),
                },
                "max_output_tokens": self.agent.max_output_tokens,
                "temperature": self.agent.temperature,
                "context_window": self.agent.context_window,
                "tool_result_detail_chars": self.agent.tool_result_detail_chars,
                "compress_async_at_ratio": self.agent.compress_async_at_ratio,
                "compress_sync_at_ratio": self.agent.compress_sync_at_ratio,
                "compress_keep_recent": self.agent.compress_keep_recent,
                "compress_summary_tokens": self.agent.compress_summary_tokens,
                "extension_dirs": [str(path) for path in self.agent.extension_dirs],
                "skill_dir": str(self.agent.skill_dir),
                "skill_index_tokens": self.agent.skill_index_tokens,
                "skill_scan_dirs": [str(path) for path in self.agent.skill_scan_dirs],
                "workspace_root": str(self.agent.workspace_root),
                "cli_bin_dir": str(self.agent.cli_bin_dir),
                "tool_timeout_seconds": self.agent.tool_timeout_seconds,
                "tool_round_timeout_seconds": self.agent.tool_round_timeout_seconds,
                "max_run_steps": self.agent.max_run_steps,
                "max_run_seconds": self.agent.max_run_seconds,
                "llm_max_attempts": self.agent.llm_max_attempts,
                "llm_retry_delay": self.agent.llm_retry_delay,
            },
            "supervisor": {
                "invoke_interval_seconds": self.supervisor.invoke_interval_seconds,
                "invoke_timeout_seconds": self.supervisor.invoke_timeout_seconds,
                "shutdown_timeout_seconds": self.supervisor.shutdown_timeout_seconds,
            },
        }


__all__ = [
    "AgentConfig",
    "ManagerConfig",
    "MiddlewareConfig",
    "ModelProviderConfig",
    "OathConfig",
    "ServerConfig",
    "SupervisorConfig",
]
