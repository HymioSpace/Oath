"""客户端带来的会话上下文：按 scope 增量覆盖维护，读的时候裁掉过期项。

这份 kv 不是模型上下文，也不参与鉴权。它只有一个消费者 —— 通过 CLI server 调用的业务
命令，拿到后自己决定怎么用。
"""

from __future__ import annotations

from typing import Final

from core.journal.runtime_store import State
from core.utils.encoding import JsonObject, JsonValue

#: 一个 key 多久没人碰就作废。写死而不是配置：作废是这份数据能被信任的前提，
#: 每个客户端都按同一个值算。
TTL_SECONDS: Final = 24 * 60 * 60

#: scope 状态里那张表的形状：每个 key 记值与最后一次被写入的时刻。
Table = dict[str, JsonObject]

#: 表在 scope 状态里的位置。整张表装在一个 meta key 下 —— 事务不允许删 key，
#: 覆盖写同一个 key 才裁得掉过期项。
_KEY: Final = "user_context"


def table_of(state: State) -> Table:
    """scope 状态里那张表。

    形状不对就当空的：读不出东西不该挡住输入落盘，也不该挡住 CLI 调用。
    """
    existing = state["meta"].get(_KEY)
    return existing if isinstance(existing, dict) else {}


def merge(existing: Table, incoming: JsonObject, *, now: float) -> Table:
    """把一条消息带来的 kv 覆盖进去。消息没带的 key 原样留着。

    同名 key 直接覆盖并更新时间 —— 客户端说的就是最新值。过期项在这里顺手裁掉：整张表
    覆盖写同一个 meta key（事务不允许删 key），被裁掉的部分跟着这次写入一起落盘。
    """
    merged = _fresh(existing, now=now)
    for key, value in incoming.items():
        merged[key] = {"value": value, "at": now}
    return merged


def current(existing: Table, *, now: float) -> JsonObject:
    """读给业务命令的那一份：过期项不在里面，值也摊平。"""
    return {key: entry["value"] for key, entry in _fresh(existing, now=now).items()}


def _fresh(existing: Table, *, now: float) -> Table:
    return {key: entry for key, entry in existing.items() if _kept(entry, now=now)}


def _kept(entry: JsonValue, *, now: float) -> bool:
    """形状不对或没有时间的项当过期处理 —— 读不出时间的值不值得信。"""
    if not isinstance(entry, dict):
        return False
    at = entry.get("at")
    return isinstance(at, (int, float)) and now - float(at) < TTL_SECONDS


__all__ = ["TTL_SECONDS", "Table", "current", "merge", "table_of"]
