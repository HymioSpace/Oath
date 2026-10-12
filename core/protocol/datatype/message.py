"""`Message` —— LLM 对话历史里的一条消息。

模型适配层、agent runtime、客户端的会话视图都要用它，journal 只是它的一个
生产者。字段用 `role` / `content` 这套通用名，不带任何一家 provider 的字段名。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    content: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转成 provider 请求里的形状。`extra` 里的键原样带出去。"""
        return {"role": self.role, "content": self.content, **self.extra}


__all__ = ["Message"]
