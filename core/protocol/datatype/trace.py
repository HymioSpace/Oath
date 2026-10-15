"""`Span` —— OpenTelemetry 的观测数据形状。

trace_id / span_id / parent_span_id / name / kind / start / end / status /
attributes / events / links，字段与 OTel 的 span 数据模型一一对应。**这不是 OTLP
的线上编码**：导出时还要一层映射（属性类型、`status` 的 code 与 message、trace
state），换的是怎么送出去，不是送什么。

时间是 unix 纳秒的整数，和 OTel 的 `fixed64` 一样。`end` 可以是 `None`：那表示这个
span 还没结束，进程就是在中途没的。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Final

from core.utils.encoding import JsonObject

#: trace_id 与 span_id 的长度（hex 字符数），与 OTel 一致。
TRACE_ID_LENGTH: Final = 32
SPAN_ID_LENGTH: Final = 16

_HEX = re.compile(r"\A[0-9a-f]+\Z")


class SpanKind:
    """span 的角色。core 只用前两个，其余留给以后的 producer / consumer。"""

    INTERNAL: Final = "internal"
    CLIENT: Final = "client"

    ALL: Final = frozenset({INTERNAL, CLIENT})


class SpanStatus:
    """span 的结果。`UNSET` 是「还没写完」，不是「成功也不是失败」。"""

    UNSET: Final = "unset"
    OK: Final = "ok"
    ERROR: Final = "error"

    ALL: Final = frozenset({UNSET, OK, ERROR})


def new_trace_id() -> str:
    """32 位小写 hex，和 OTel 的 128 位 trace id 一样宽。"""
    return uuid.uuid4().hex


def new_span_id() -> str:
    return uuid.uuid4().hex[:SPAN_ID_LENGTH]


def is_trace_id(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == TRACE_ID_LENGTH and _HEX.match(value) is not None
    )


def is_span_id(value: object) -> bool:
    return isinstance(value, str) and len(value) == SPAN_ID_LENGTH and _HEX.match(value) is not None


@dataclass(frozen=True, slots=True)
class SpanEvent:
    """span 内按时间排的一件事。原始请求与响应就是它，不是 attribute。"""

    name: str
    unix_nano: int
    attributes: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return {"name": self.name, "unix_nano": self.unix_nano, "attributes": self.attributes}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SpanEvent:
        return cls(
            name=str(raw["name"]),
            unix_nano=int(raw["unix_nano"]),
            attributes=dict(raw.get("attributes") or {}),
        )


@dataclass(frozen=True, slots=True)
class SpanLink:
    """一条因果关系，指向不在自己父链上的 span。

    有因果但不在同一棵父子树里 —— 典型是「这一步被上一步的工具结果唤醒」，而那两步
    在时间上是先后而不是嵌套。`trace_id` 是必备的：被指向的 span 常常不在自己这条
    trace 里。
    """

    trace_id: str
    span_id: str
    attributes: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not is_trace_id(self.trace_id):
            raise ValueError(
                f"link 的 trace_id 必须是 {TRACE_ID_LENGTH} 位小写 hex: {self.trace_id!r}"
            )
        if not is_span_id(self.span_id):
            raise ValueError(
                f"link 的 span_id 必须是 {SPAN_ID_LENGTH} 位小写 hex: {self.span_id!r}"
            )

    def to_dict(self) -> JsonObject:
        return {"trace_id": self.trace_id, "span_id": self.span_id, "attributes": self.attributes}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SpanLink:
        return cls(
            trace_id=str(raw["trace_id"]),
            span_id=str(raw["span_id"]),
            attributes=dict(raw.get("attributes") or {}),
        )


@dataclass(frozen=True, slots=True)
class Span:
    """一次可观测动作。

    `parent_span_id` 为 `None` 是 trace 的顶层 span。core 里每个 step span 都是顶层，
    它的 llm span 是子 span —— step 之间用 trace_id 串，不套成一棵树：第二步发生在
    第一步结束之后，把它塞进第一步里会让耗时读起来是错的。step 之间的先后关系靠
    `links` 补。
    """

    trace_id: str
    span_id: str
    name: str
    start_unix_nano: int
    end_unix_nano: int | None = None
    parent_span_id: str | None = None
    kind: str = SpanKind.INTERNAL
    status: str = SpanStatus.UNSET
    attributes: JsonObject = field(default_factory=dict)
    events: tuple[SpanEvent, ...] = ()
    links: tuple[SpanLink, ...] = ()

    def __post_init__(self) -> None:
        # id 的长度在构造时校验：写错了当场报错，好过读 trace 的人对着一个不合法的
        # id 发呆。
        if not is_trace_id(self.trace_id):
            raise ValueError(f"trace_id 必须是 {TRACE_ID_LENGTH} 位小写 hex: {self.trace_id!r}")
        if not is_span_id(self.span_id):
            raise ValueError(f"span_id 必须是 {SPAN_ID_LENGTH} 位小写 hex: {self.span_id!r}")
        if self.parent_span_id is not None and not is_span_id(self.parent_span_id):
            raise ValueError(
                f"parent_span_id 必须是 {SPAN_ID_LENGTH} 位小写 hex: {self.parent_span_id!r}"
            )
        if self.kind not in SpanKind.ALL:
            raise ValueError(f"未知的 span kind: {self.kind!r}")
        if self.status not in SpanStatus.ALL:
            raise ValueError(f"未知的 span status: {self.status!r}")

    @property
    def duration_ms(self) -> float | None:
        """span 自己的耗时。还没结束就是 `None`。"""
        if self.end_unix_nano is None:
            return None
        return (self.end_unix_nano - self.start_unix_nano) / 1_000_000

    def to_dict(self) -> JsonObject:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "kind": self.kind,
            "start_unix_nano": self.start_unix_nano,
            "end_unix_nano": self.end_unix_nano,
            "status": self.status,
            "attributes": self.attributes,
            "events": [event.to_dict() for event in self.events],
            "links": [link.to_dict() for link in self.links],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Span:
        """读回一条 span。字段不对就抛 `KeyError` / `ValueError` —— 调用方按坏行处理。"""
        parent = raw.get("parent_span_id")
        return cls(
            trace_id=str(raw["trace_id"]),
            span_id=str(raw["span_id"]),
            name=str(raw["name"]),
            start_unix_nano=int(raw["start_unix_nano"]),
            end_unix_nano=(None if raw.get("end_unix_nano") is None else int(raw["end_unix_nano"])),
            parent_span_id=None if parent is None else str(parent),
            kind=str(raw.get("kind", SpanKind.INTERNAL)),
            status=str(raw.get("status", SpanStatus.UNSET)),
            attributes=dict(raw.get("attributes") or {}),
            events=tuple(SpanEvent.from_dict(event) for event in raw.get("events") or ()),
            links=tuple(SpanLink.from_dict(link) for link in raw.get("links") or ()),
        )


__all__ = [
    "SPAN_ID_LENGTH",
    "TRACE_ID_LENGTH",
    "Span",
    "SpanEvent",
    "SpanKind",
    "SpanLink",
    "SpanStatus",
    "is_span_id",
    "is_trace_id",
    "new_span_id",
    "new_trace_id",
]
