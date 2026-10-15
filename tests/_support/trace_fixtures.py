"""trace 的测试替身。"""

from __future__ import annotations

import threading

from core.protocol.datatype.scope import Scope
from core.protocol.datatype.trace import Span
from core.protocol.interface.trace import TraceStore


class RecordingTraceStore(TraceStore):
    """内存里的 trace store。

    默认用它而不是文件后端：大部分测试要的是「有没有写」，不是「写到磁盘了没有」。
    线程安全 —— span 是 Brain 线程写的，断言可能在测试线程读。
    """

    def __init__(self) -> None:
        self.written: list[tuple[Scope, Span]] = []
        self.fail = False
        self._lock = threading.Lock()

    def append(self, scope: Scope, span: Span) -> None:
        if self.fail:
            raise OSError("trace 写不进去（测试）")
        with self._lock:
            self.written.append((scope, span))

    def trace_ids(self, scope: Scope) -> tuple[str, ...]:
        with self._lock:
            seen = {span.trace_id for written, span in self.written if written == scope}
        return tuple(sorted(seen))

    def spans(self, scope: Scope, trace_id: str) -> tuple[Span, ...]:
        with self._lock:
            latest = {
                span.span_id: span
                for written, span in self.written
                if written == scope and span.trace_id == trace_id
            }
        return tuple(sorted(latest.values(), key=lambda span: span.start_unix_nano))

    def by_name(self, scope: Scope, name: str) -> tuple[Span, ...]:
        return tuple(
            span for written, span in self.written if written == scope and span.name == name
        )


class BrokenTraceStore(RecordingTraceStore):
    """每次写都抛。用来验证「写 trace 失败不影响推理」。"""

    def __init__(self) -> None:
        super().__init__()
        self.fail = True


__all__ = ["BrokenTraceStore", "RecordingTraceStore"]
