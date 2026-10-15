"""span 的存放处：只追加、最后一条赢、坏行只丢自己。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.protocol.datatype.scope import Scope
from core.protocol.datatype.trace import (
    Span,
    SpanEvent,
    SpanLink,
    SpanStatus,
    new_trace_id,
)
from core.trace.impl import FileTraceStore

SCOPE = Scope("u1", "s1")
OTHER = Scope("u1", "s2")


def span(trace_id: str, span_id: str, *, start: int = 1, **kwargs: object) -> Span:
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        name=str(kwargs.pop("name", "invoke_agent")),
        start_unix_nano=start,
        **kwargs,  # type: ignore[arg-type]
    )


def test_a_span_survives_a_round_trip(tmp_path: Path) -> None:
    store = FileTraceStore(tmp_path)
    trace_id, other = new_trace_id(), new_trace_id()
    store.append(
        SCOPE,
        span(
            trace_id,
            "0123456789abcdef",
            end_unix_nano=3,
            status=SpanStatus.OK,
            attributes={"oath.step.id": "s"},
            events=(SpanEvent("gen_ai.request", 2, {"body": '{"a":1}'}),),
            links=(SpanLink(other, "fedcba9876543210", {"oath.step.id": "prev"}),),
        ),
    )

    [read] = store.spans(SCOPE, trace_id)
    assert read.trace_id == trace_id
    assert read.status == SpanStatus.OK
    assert read.attributes["oath.step.id"] == "s"
    assert [event.name for event in read.events] == ["gen_ai.request"]
    assert read.events[0].attributes["body"] == '{"a":1}'
    # link 自带 trace_id：被指向的 span 常常不在自己这条 trace 里。
    assert read.links == (SpanLink(other, "fedcba9876543210", {"oath.step.id": "prev"}),)


def test_the_last_line_wins_for_one_span_id(tmp_path: Path) -> None:
    """认领写一次、终态再写一次 —— 读的时候只认后写的。"""
    store = FileTraceStore(tmp_path)
    trace_id = new_trace_id()
    store.append(SCOPE, span(trace_id, "0123456789abcdef"))
    store.append(SCOPE, span(trace_id, "0123456789abcdef", end_unix_nano=9, status="ok"))

    [read] = store.spans(SCOPE, trace_id)
    assert read.end_unix_nano == 9
    assert read.status == SpanStatus.OK


def test_spans_come_back_in_start_order(tmp_path: Path) -> None:
    store = FileTraceStore(tmp_path)
    trace_id = new_trace_id()
    for index, start in enumerate([30, 10, 20]):
        store.append(SCOPE, span(trace_id, f"{index:016x}", start=start))

    assert [s.start_unix_nano for s in store.spans(SCOPE, trace_id)] == [10, 20, 30]


def test_a_scope_only_sees_its_own_traces(tmp_path: Path) -> None:
    store = FileTraceStore(tmp_path)
    mine, theirs = new_trace_id(), new_trace_id()
    store.append(SCOPE, span(mine, "0123456789abcdef"))
    store.append(OTHER, span(theirs, "0123456789abcdef"))

    assert store.trace_ids(SCOPE) == (mine,)
    assert store.trace_ids(OTHER) == (theirs,)
    assert store.spans(SCOPE, theirs) == ()


def test_an_unknown_or_malformed_trace_id_is_empty(tmp_path: Path) -> None:
    store = FileTraceStore(tmp_path)
    store.append(SCOPE, span(new_trace_id(), "0123456789abcdef"))

    assert store.trace_ids(Scope("u1", "never-used")) == ()
    assert store.spans(SCOPE, "../escape") == ()
    assert store.spans(SCOPE, new_trace_id()) == ()


def test_a_broken_line_does_not_hide_the_rest(tmp_path: Path) -> None:
    store = FileTraceStore(tmp_path)
    trace_id = new_trace_id()
    store.append(SCOPE, span(trace_id, "0000000000000000", start=10))
    path = tmp_path / SCOPE.user_id / SCOPE.session_id / f"{trace_id}.jsonl"
    with open(path, "a", encoding="utf-8") as stream:
        stream.write("{不是 json\n")
        stream.write(json.dumps({"trace_id": trace_id}) + "\n")  # 缺字段
    store.append(SCOPE, span(trace_id, "0000000000000001", start=20))

    assert [s.span_id for s in store.spans(SCOPE, trace_id)] == [
        "0000000000000000",
        "0000000000000001",
    ]


def test_a_stray_file_in_the_directory_is_not_a_trace(tmp_path: Path) -> None:
    store = FileTraceStore(tmp_path)
    trace_id = new_trace_id()
    store.append(SCOPE, span(trace_id, "0123456789abcdef"))
    (tmp_path / SCOPE.user_id / SCOPE.session_id / "README.txt").write_text("x")

    assert store.trace_ids(SCOPE) == (trace_id,)


def test_the_most_recently_written_trace_comes_first(tmp_path: Path) -> None:
    """排序看文件修改时间：失败重试补写的旧 trace 会回到前面，而不是留在创建时的位置。"""
    store = FileTraceStore(tmp_path)
    older, newer = new_trace_id(), new_trace_id()
    store.append(SCOPE, span(older, "0123456789abcdef"))
    store.append(SCOPE, span(newer, "0123456789abcdef"))
    os.utime(_file(tmp_path, older), (1_000_000, 1_000_000))
    store.append(SCOPE, span(older, "0123456789abcdef", end_unix_nano=9))

    assert store.trace_ids(SCOPE) == (older, newer)


def _file(tmp_path: Path, trace_id: str) -> Path:
    return tmp_path / SCOPE.user_id / SCOPE.session_id / f"{trace_id}.jsonl"


@pytest.mark.parametrize(
    "field",
    [
        {"trace_id": "abc"},
        {"span_id": "not-hex-at-all-"},
        {"parent_span_id": "0123"},
        {"kind": "producer"},
        {"status": "maybe"},
    ],
)
def test_a_malformed_id_or_enum_is_rejected_at_construction(field: dict[str, str]) -> None:
    kwargs: dict[str, object] = {"trace_id": new_trace_id(), "span_id": "0123456789abcdef"}
    kwargs.update(field)

    with pytest.raises(ValueError):
        Span(name="invoke_agent", start_unix_nano=1, **kwargs)  # type: ignore[arg-type]
