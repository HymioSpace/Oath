"""文件后端的 trace store。布局与 journal、detail 一致，一个 trace 一个文件。

    <root>/<user_id>/<session_id>/<trace_id>.jsonl

一行一条 span，只追加。**不 fsync**：trace 不参与恢复，写进页缓存就算成功 ——
崩溃丢几条 span，比推理被 fsync 拖住划算。

trace_id 是 core 生成的 32 位 hex，不接受外部给的路径片段；读的时候照样校验一次，
目录里混进别的东西也不会被当 trace 打开。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from core.protocol.datatype.scope import Scope
from core.protocol.datatype.trace import Span, is_trace_id
from core.protocol.interface.trace import TraceStore

_log = logging.getLogger(__name__)

_SUFFIX = ".jsonl"


class FileTraceStore(TraceStore):
    """一个 trace 一个 JSONL 文件，同一个 span_id 后写的覆盖先写的。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    def append(self, scope: Scope, span: Span) -> None:
        directory = self._directory(scope)
        directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(span.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with open(directory / f"{span.trace_id}{_SUFFIX}", "a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def trace_ids(self, scope: Scope) -> tuple[str, ...]:
        directory = self._directory(scope)
        if not directory.is_dir():
            return ()
        found: list[tuple[float, str]] = []
        for entry in directory.iterdir():
            name = entry.name
            if not name.endswith(_SUFFIX) or not entry.is_file():
                continue
            trace_id = name[: -len(_SUFFIX)]
            if not is_trace_id(trace_id):
                continue
            found.append((entry.stat().st_mtime, trace_id))
        return tuple(trace_id for _, trace_id in sorted(found))

    def spans(self, scope: Scope, trace_id: str) -> tuple[Span, ...]:
        if not is_trace_id(trace_id):
            return ()
        path = self._directory(scope) / f"{trace_id}{_SUFFIX}"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, ValueError):
            return ()
        # 同一个 span_id 写过多遍（开始一次、终态一次）：最后一条赢。
        latest: dict[str, Span] = {}
        for line in lines:
            if not line.strip():
                continue
            try:
                span = Span.from_dict(json.loads(line))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                # 坏行只丢它自己：一次写坏了不该让整条 trace 读不出来。
                _log.warning("跳过损坏的 span 行: %s", error)
                continue
            if span.trace_id != trace_id:
                continue
            latest[span.span_id] = span
        return tuple(sorted(latest.values(), key=lambda span: span.start_unix_nano))

    def _directory(self, scope: Scope) -> Path:
        return self._root / scope.user_id / scope.session_id


__all__ = ["FileTraceStore"]
