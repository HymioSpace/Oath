"""user_context：按 scope 增量覆盖维护，24 小时不用就作废。

用真实 Journal —— 这里要验的正是「落盘的表能原样取回来」，替身绕过了恢复那一段。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Generator
from pathlib import Path
from typing import cast

import pytest

from bootstrap.runtime import Runtime
from core.context.user_context import TTL_SECONDS, Table, current, merge
from core.journal.impl import FileJournal
from core.journal.runtime_store import RuntimeStore, State
from core.protocol.datatype.config import OathConfig
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import Command, CommandType
from core.protocol.error import WireProtocolError
from core.utils.encoding import JsonObject, JsonValue
from tests._support.intake_fixtures import ScriptedProvider, until

SCOPE = Scope("u1", "s1")
NOW = 1_760_000_000.0


def table(state: State) -> Table:
    stored = state["meta"].get("user_context")
    assert isinstance(stored, dict)
    return cast(Table, stored)


def consumed(runtime: Runtime) -> bool:
    """这条输入已经被 user consumer 处理过 —— 合并就在那一步里，早于进 buffer。"""
    entries = list(runtime.ctx.store.read(SCOPE)["inputs"].values())
    return bool(entries) and all(e["phase"] != "accepted" for e in entries)


def test_a_new_message_keeps_the_keys_it_did_not_mention() -> None:
    """一条消息给的是增量。全量覆盖会让用户每句话都要重复所有上下文。"""
    first = merge({}, {"project": "hymio", "env": "dev"}, now=NOW)
    second = merge(first, {"project": "oath"}, now=NOW + 10)

    assert current(second, now=NOW + 10) == {"project": "oath", "env": "dev"}


def test_the_same_key_is_overwritten_and_its_time_refreshed() -> None:
    """覆盖的是客户端说的最新值，时间跟着走 —— 否则用过一次的 key 会白等 24 小时。"""
    stale: Table = {"project": {"value": "hymio", "at": NOW}}
    later = NOW + TTL_SECONDS + 1

    merged = merge(stale, {"project": "oath"}, now=later)

    assert merged["project"] == {"value": "oath", "at": later}


def test_a_key_nobody_touched_again_is_gone_after_a_day() -> None:
    """过期在写入时顺手裁掉：整张表覆盖写同一个 meta key，事务不允许删 key。"""
    stale: Table = {"env": {"value": "dev", "at": NOW}}

    merged = merge(stale, {"project": "oath"}, now=NOW + TTL_SECONDS + 1)

    assert current(merged, now=NOW + TTL_SECONDS + 1) == {"project": "oath"}


def test_reading_also_drops_what_expired_while_nothing_was_written() -> None:
    """没有清理线程 —— 没人再提某个 key 时，它也要在读的时候消失。"""
    merged = merge({}, {"env": "dev"}, now=NOW)

    assert current(merged, now=NOW) == {"env": "dev"}
    assert current(merged, now=NOW + TTL_SECONDS) == {}


def test_a_row_without_a_time_is_not_trusted() -> None:
    """读不出时间的值没有保质期可言 —— 形状不对的一律当过期。"""
    broken = cast(Table, {"a": {"value": 1}, "b": "not-a-row"})

    assert current(broken, now=NOW) == {}


# -- 装配起来的那条路径 -----------------------------------------------------


@pytest.fixture
def runtime(tmp_path: Path) -> Generator[Runtime, None, None]:
    runtime = Runtime(
        OathConfig(
            data_root=tmp_path,
            credentials={"synthetic-test-token": (SCOPE.user_id, SCOPE.session_id)},
        ),
        loop=asyncio.new_event_loop(),
        provider=ScriptedProvider(),
    )
    yield runtime
    runtime.close()


def send(runtime: Runtime, context: JsonObject, id_: str) -> None:
    runtime.submit(
        Command(
            id_,
            CommandType.USER_MESSAGE,
            SCOPE.session_id,
            {"content": "查一下", "user_context": context},
        ),
        SCOPE,
    )


def test_the_context_of_a_message_lands_in_that_scope(runtime: Runtime) -> None:
    send(runtime, {"project": "hymio"}, "u1")

    until(lambda: consumed(runtime))
    assert current(table(runtime.ctx.store.read(SCOPE)), now=time.time()) == {"project": "hymio"}
    # 另一个 session 连表都没有 —— kv 不跨 scope。
    assert runtime.ctx.store.read(Scope("u1", "s2"))["meta"].get("user_context") is None


def test_a_later_message_only_overwrites_what_it_carries(runtime: Runtime) -> None:
    send(runtime, {"project": "hymio", "env": "dev"}, "u1")
    until(lambda: consumed(runtime))
    send(runtime, {"project": "oath"}, "u2")
    until(
        lambda: current(table(runtime.ctx.store.read(SCOPE)), now=time.time())["project"] == "oath"
    )

    assert current(table(runtime.ctx.store.read(SCOPE)), now=time.time()) == {
        "project": "oath",
        "env": "dev",
    }


def test_the_context_survives_a_restart(runtime: Runtime, tmp_path: Path) -> None:
    """它活在 journal 里，不在内存里 —— 重启后 CLI 拿到的还是同一份。"""
    send(runtime, {"project": "hymio"}, "u1")
    until(lambda: consumed(runtime))
    runtime.close()

    with FileJournal(tmp_path / "journal") as journal:
        recovered = RuntimeStore(journal).read(SCOPE)

    assert current(table(recovered), now=time.time()) == {"project": "hymio"}


def test_a_message_without_a_usable_context_is_refused_whole(runtime: Runtime) -> None:
    """形状不对时整帧拒掉，不做部分接纳 —— 半份上下文比没有更难解释。"""
    for bad in cast("tuple[JsonValue, ...]", (None, "hymio", {"": "x"})):
        with pytest.raises(WireProtocolError):
            runtime.submit(
                Command(
                    "u1",
                    CommandType.USER_MESSAGE,
                    SCOPE.session_id,
                    {"content": "查一下", "user_context": bad},
                ),
                SCOPE,
            )
    assert runtime.ctx.store.read(SCOPE)["inputs"] == {}


def test_a_message_carrying_no_context_writes_nothing(runtime: Runtime) -> None:
    """空的不动表：没有新事实就别制造一次状态写入。"""
    runtime.ctx.store.transact(SCOPE, "test.seed", lambda s: s["meta"].update(user_context={}))

    send(runtime, {}, "u1")
    until(lambda: consumed(runtime))

    assert runtime.ctx.store.read(SCOPE)["meta"].get("user_context") == {}
