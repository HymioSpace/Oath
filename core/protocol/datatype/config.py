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

    #: 项目自己的技能目录。进 system prompt 末尾的技能索引。
    skill_dir: Path = Path("./extensions/skills")
    #: 技能索引进 system prompt 的 token 预算。装不下就按优先级丢。
    skill_index_tokens: int = 1024
    #: 额外扫到的技能目录。它们只能被 skill 工具搜到，不进 system prompt。
    skill_scan_dirs: tuple[Path, ...] = ()

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
    """两个 server 的监听参数：WebSocket 收 command，HTTP 供 biz 只读查询。"""

    host: str = "127.0.0.1"
    port: int = 8765
    #: biz server 的端口。整个 biz 一个端口，每条查询是其中一个 path。与 WebSocket
    #: 分开：查询不是 command，不该占会话连接。
    http_port: int = 8766
    #: 每条连接的发送队列长度。满了就断开那条连接，不拖住 core。
    send_queue_size: int = 256
    #: 一条连接多久没有任何 command 就断开。
    idle_timeout_seconds: float = 300.0
    #: 同时接受多少条连接。
    max_connections: int = 32


@dataclass(frozen=True, slots=True)
class OathConfig:
    """一份完整配置。"""

    data_root: Path = field(default_factory=lambda: Path("./data"))
    server: ServerConfig = field(default_factory=ServerConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    #: v1 的演示身份：`credential` → `(user_id, session_id)`。生产由 Hymio 提供。
    credentials: dict[str, tuple[str, str]] = field(default_factory=dict)

    def credential_for(self, token: str) -> tuple[str, str] | None:
        """查一份 credential 对应的身份。认不出来返回 `None`。"""
        return self.credentials.get(token)

    def to_json(self) -> dict[str, Any]:
        """完整配置的 dict 形态。**不含凭证的值** —— 只有环境变量名。"""
        return {
            "data_root": str(self.data_root),
            "server": {
                "host": self.server.host,
                "port": self.server.port,
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
                "skill_dir": str(self.agent.skill_dir),
                "skill_index_tokens": self.agent.skill_index_tokens,
                "skill_scan_dirs": [str(path) for path in self.agent.skill_scan_dirs],
                "tool_timeout_seconds": self.agent.tool_timeout_seconds,
                "tool_round_timeout_seconds": self.agent.tool_round_timeout_seconds,
                "max_run_steps": self.agent.max_run_steps,
                "max_run_seconds": self.agent.max_run_seconds,
                "llm_max_attempts": self.agent.llm_max_attempts,
                "llm_retry_delay": self.agent.llm_retry_delay,
            },
        }


__all__ = ["AgentConfig", "ModelProviderConfig", "OathConfig", "ServerConfig"]
