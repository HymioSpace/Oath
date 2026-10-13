"""`ModelProvider` —— core 与模型之间的那一层。

core 只知道两件事：发出去一个 `LlmRequest`，拿回来一个 `LlmResponse`。请求体怎么拼、
响应怎么解、流式怎么切、厂商特有的参数叫什么，全是这一层的事。

**这一层不许知道 agent 怎么跑。** 它不落盘、不派发工具、不决定下一步 ——
`AgentBrain` 拿到 `LlmResponse` 之后做什么，跟它无关。换一家 provider 时
`AgentBrain` 一行不用改。

v1 只有一个实现（`core/llm/openai_provider.py`，OpenAI 兼容接口），所以没有注册表、
没有插件加载器。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype.llm import LlmRequest, LlmResponse


class ModelProvider(ABC):
    """一次非流式模型调用。"""

    @abstractmethod
    def complete(self, request: LlmRequest) -> LlmResponse:
        """发一次请求，等一次完整响应。

        **同步、阻塞。** 调用方（`AgentBrain`）在自己那个 session 的消费线程上跑它，
        所以阻塞的是那一条 session，不是整个 runtime。

        失败一律抛 `LlmError`：网络错误、超时、非 2xx、响应体形状不对。**没有返回值
        表示失败的形态** —— 一个 `LlmResponse` 一定是模型真说过的话。
        """
        ...


__all__ = ["ModelProvider"]
