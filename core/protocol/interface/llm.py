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
from collections.abc import Callable
from typing import Final

from core.protocol.datatype.llm import LlmRequest, LlmResponse


class DeltaChannel:
    """delta 走的是哪一路。

    `REASONING` 与 `CONTENT` 分开：模型可能先想后答，也可能两路交错。合在一起发，
    客户端就没法把「正在想」和「正在答」分开显示。
    """

    #: 模型输出的一部分，assistant 会说的话。
    CONTENT: Final = "content"
    #: 模型的思考过程。**不是所有厂家都给。**
    REASONING: Final = "reasoning"


#: 收到一段增量文本。`(channel, text)` —— 没有 scope：调用方闭包里已经绑好了。
DeltaSink = Callable[[str, str], None]


class ModelProvider(ABC):
    """一次模型调用。"""

    @abstractmethod
    def complete(self, request: LlmRequest, *, on_delta: DeltaSink | None = None) -> LlmResponse:
        """发一次请求，等一次完整响应。

        **同步、阻塞。** 调用方（`AgentBrain`）在自己那个 session 的消费线程上跑它，
        所以阻塞的是那一条 session，不是整个 runtime。

        `on_delta` 给了就流式：收到一段就调一次，**返回的仍然是完整的 `LlmResponse`**。
        这一点是刻意的 —— 落盘、`tool.call` 派发、`system.agent_step` 收尾全在返回值之后，
        流式只是让等待期间客户端有东西可看，不改动任何一条记录。给了 `None` 就是一次
        普通调用，`stream=false`，不发任何回调。

        **回调只带 `content` 与 `reasoning`。** `tool_calls` 不流式：`arguments` 是增量
        JSON，中途不是合法对象，收集完才能解析 —— 半截参数没有任何用处。

        回调里的异常不往外传：投递失败不该把一次成功的模型调用变成失败。调用方
        自己决定吞掉还是记一笔。

        失败一律抛 `LlmError`：网络错误、超时、非 2xx、响应体形状不对。**没有返回值
        表示失败的形态** —— 一个 `LlmResponse` 一定是模型真说过的话。
        """
        ...


__all__ = ["DeltaChannel", "DeltaSink", "ModelProvider"]
