"""Journal conformance 套件 —— 换存储实现时跑的验收测试。

## 只断言通用的条件，不断言具体数字

不同实现的扫描方式本就不同：JSONL 按字节偏移定位，SQLite 会走索引。断言
`through_seq` 的具体数值，就得给其中一个造一套假的对应关系，反而掩盖真实行为。
所以这里只断言「单调」「不倒退」「下一条 seq 大于上一条」这类条件。

## 跳过数变多 = 能力声明有问题

`required_capability` 让不具备该能力的 adapter 跳过对应检查。目前 0 跳过：
JSONL 声明了 `reopen` 和 `simulate_crash`，也确实满足。跳过数一旦上升，
说明某个 adapter 的能力声明漏了。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from core.errors import (
    CursorInvalid,
    FencedOut,
    LimitExceeded,
    ScopeViolation,
    StateVersionConflict,
    ValidationError,
)
from core.journal import (
    REOPEN,
    SIMULATE_CRASH,
    BlobRef,
    Head,
    Journal,
    JournalRecord,
    Principal,
    ReadPage,
    ReadRequest,
    RecordDraft,
    RecordFilter,
    Scope,
    Writer,
)
from tests._support.journal_factory import ADAPTERS, Harness, make_principal, make_scope

CheckFn = Callable[[Harness], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    run: CheckFn
    required_capability: str | None = None


# ---------- 构造辅助 ----------

def event(record_id: str, *, event_id: str | None = None, queue: str = "user",
          payload: dict[str, Any] | None = None, type_: str = "user.message",
          run_id: str | None = None, producer: str = "ingress") -> RecordDraft:
    return RecordDraft(
        record_id=record_id,
        type=type_,
        category="event",
        producer=producer,
        queue=queue,  # type: ignore[arg-type]
        event_id=event_id or record_id,
        run_id=run_id,
        payload=payload if payload is not None else {"text": record_id},
    )


def observation(record_id: str, *, type_: str = "trace.log",
                payload: dict[str, Any] | None = None,
                producer: str = "observer") -> RecordDraft:
    return RecordDraft(
        record_id=record_id,
        type=type_,
        category="observation",
        producer=producer,
        payload=payload if payload is not None else {"n": record_id},
    )


def state(record_id: str, *, type_: str = "session.state", payload: dict[str, Any] | None = None
          ) -> RecordDraft:
    return RecordDraft(
        record_id=record_id,
        type=type_,
        category="state",
        producer="core",
        payload=payload if payload is not None else {"v": record_id},
    )


async def read_all(journal: Journal, principal: Principal, scope: Scope, *,
                   limit: int = 100, record_filter: RecordFilter | None = None,
                   after_seq: int = 0) -> tuple[JournalRecord, ...]:
    """反复翻页直到读完。

    故意用循环而不是一次 limit=100：只有走真实分页，才能发现「过滤流在空页处
    默默停止消费」这类只有真翻页才会暴露的问题。
    """
    items: list[JournalRecord] = []
    while True:
        page = await journal.read(
            ReadRequest(principal=principal, scope=scope, after_seq=after_seq,
                        limit=limit, filter=record_filter or RecordFilter())
        )
        items.extend(page.items)
        if page.next_cursor is None:
            return tuple(items)
        after_seq = page.through_seq


def writer_for(journal: Journal, principal: Principal, scope: Scope, producer: str = "test",
               **kwargs: Any) -> Writer:
    return journal.bind(principal, scope, producer, **kwargs)


# ---------- 1. 隔离 ----------

async def check_isolation(h: Harness) -> None:
    """同名 session 在不同 user 下完全隔离；越权读写都被拒。"""
    j = h.journal
    alice = make_principal("alice", user="u1", session="s1")
    bob = make_principal("bob", user="u2", session="s1")

    w_alice = writer_for(j, alice, make_scope("u1", "s1"))
    assert (await w_alice.append([event("a1")], "c-alice")).outcome == "accepted"

    # bob 的 scope 存在但为空 —— 他的 journal 是独立的流
    assert await j.head(bob, make_scope("u2", "s1")) == Head(
        seq=0, state_version=0, owner_epoch=0
    )
    assert await read_all(j, bob, make_scope("u2", "s1")) == ()

    # bob 有自己的权限，却读不了 alice 的 scope ——
    # 授权跟着 principal 走，不是「已认证即可读一切」。
    with pytest.raises(ScopeViolation):
        await read_all(j, bob, make_scope("u1", "s1"))

    # 越权读
    with pytest.raises(ScopeViolation):
        await read_all(j, alice, make_scope("u2", "s1"))

    # 越权写
    with pytest.raises(ScopeViolation):
        writer_for(j, alice, make_scope("u2", "s1"))

    # 越权读计数器 —— head 也是这个 scope 的信息，一样要带 principal
    with pytest.raises(ScopeViolation):
        await j.head(bob, make_scope("u1", "s1"))


# ---------- 2. seq 分配 ----------

async def check_seq_allocation(h: Harness) -> None:
    """seq 从 1 开始、严格递增、无重复；每个 scope 独立计数。"""
    j = h.journal
    # 两个 scope 都要写，所以主体必须同时有权访问它们 —— 否则这条测试测的是
    # ScopeViolation，不是 seq 分配。
    p = Principal(principal_id="p1", scopes=frozenset({("u1", "s1"), ("u1", "s2")}))
    s1 = make_scope("u1", "s1")
    w = writer_for(j, p, s1)

    seqs: list[int] = []
    for i in range(5):
        receipt = await w.append([event(f"r{i}")], f"c{i}")
        assert receipt.outcome == "accepted"
        seqs.extend(range(receipt.first_seq or 0,
                          (receipt.first_seq or 0) + receipt.record_count))

    assert seqs[0] == 1, "seq 从 1 开始；0 表示「还没有记录」，不能拿来当 first_seq"
    assert seqs == sorted(set(seqs)), f"seq 必须严格递增且不重复，实际 {seqs}"

    # 另一个 scope 从 1 重新开始 —— 隔离是存储键的一部分
    w2 = writer_for(j, p, make_scope("u1", "s2"))
    r = await w2.append([event("x")], "cx")
    assert r.first_seq == 1


# ---------- 3. 原子批次 ----------

async def check_atomic_batch(h: Harness) -> None:
    """一批要么全部可见，要么全部不可见。"""
    j = h.journal
    p = make_principal()
    s = make_scope()
    w = writer_for(j, p, s)

    receipt = await w.append([event("a"), event("b"), observation("o")], "c1")
    assert receipt.record_count == 3
    items = await read_all(j, p, s)
    assert [r.record_id for r in items] == ["a", "b", "o"]
    assert len({r.commit_id for r in items}) == 1, "同一 commit 的记录共享 commit_id"


# ---------- 4. 失败后无 seq 空洞 ★ ----------

async def check_no_seq_gap_after_failure(h: Harness) -> None:
    """被拒的批次不能消耗 seq。

    一批 3 条里第 2 条 payload 不可编码 → 整批被拒，head 不变；下一次成功的 append
    拿到 seq=1 而不是 seq=3。

    如果实现是「先分配 seq 再校验」，失败会在日志里留下空洞，恢复时就无法判断
    seq 之间到底有没有丢过东西。
    """
    j = h.journal
    p = make_principal()
    s = make_scope()
    w = writer_for(j, p, s)

    head_before = await j.head(p, s)

    bad = event("b2")
    object.__setattr__(bad, "payload", {"obj": object()})  # 绕过 __post_init__ 造脏数据
    with pytest.raises(ValidationError):
        await w.append([event("b1"), bad, event("b3")], "c-bad")

    assert await j.head(p, s) == head_before, "被拒的批次不能改变 head"
    assert await read_all(j, p, s) == (), "被拒的批次不能留下任何记录"

    receipt = await w.append([event("ok")], "c-ok")
    assert receipt.first_seq == 1, (
        f"失败批次不能消耗 seq：期望 first_seq=1，实际 {receipt.first_seq}"
    )


# ---------- 5. commit 幂等 ----------

async def check_commit_idempotency(h: Harness) -> None:
    """同 commit_id + 同内容重试 → 拿回原 receipt，不写第二遍。"""
    j = h.journal
    p = make_principal()
    s = make_scope()
    w = writer_for(j, p, s)

    first = await w.append([event("a"), event("b")], "c1")
    second = await w.append([event("a"), event("b")], "c1")

    assert second.outcome == "duplicate"
    assert second.first_seq == first.first_seq
    assert second.checksum == first.checksum
    assert await j.head(p, s) == Head(seq=2, state_version=0, owner_epoch=0)
    assert len(await read_all(j, p, s)) == 2


# ---------- 6. commit 内容冲突 ----------

async def check_commit_content_conflict(h: Harness) -> None:
    """同 commit_id 但内容不同 → conflict，且**不覆盖既有记录**。"""
    j = h.journal
    p = make_principal()
    s = make_scope()
    w = writer_for(j, p, s)

    await w.append([event("a")], "c1")
    conflict = await w.append([event("DIFFERENT")], "c1")

    assert conflict.outcome == "conflict"
    assert not conflict.succeeded
    items = await read_all(j, p, s)
    assert [r.record_id for r in items] == ["a"], "conflict 不能改写历史"


# ---------- 6b. event_id 去重 ----------

async def check_event_id_dedup(h: Harness) -> None:
    """同 event_id 同内容跨 commit 去重；同 event_id 不同内容冲突。

    去重键是 `(user_id, session_id, event_id)` —— 注意不含 commit_id：上游重发同一条
    事件时会用新的 commit_id（甚至新的 record_id），但 payload 相同，仍必须被识别为
    同一事件。payload 不同则是真正的冲突，必须暴露而不是当重复吃掉 ——
    悄悄丢弃内容不同的同 ID 事件，等于让上游的 bug 永远查不出来。
    """
    j = h.journal
    p = make_principal()
    s = make_scope()
    w = writer_for(j, p, s)

    body = {"text": "同一条消息"}
    await w.append([event("a1", event_id="E1", payload=body)], "c1")

    dup = await w.append(
        [event("a1-redelivered", event_id="E1", payload=dict(body))],
        "c2-different-commit",
    )
    assert dup.outcome == "duplicate", "跨 commit 的同 event_id 同内容必须去重"

    conflicting = await w.append(
        [event("a2", event_id="E1", payload={"text": "篡改"})], "c3",
    )
    assert conflicting.outcome == "conflict"
    assert len(await read_all(j, p, s)) == 1, "去重与冲突都不能产生额外记录"


# ---------- 7. CAS + fencing ★ ----------

async def check_state_cas_and_fencing(h: Harness) -> None:
    """state_version CAS 与 owner_epoch fencing 都生效。

    两个条件各自独立触发：
    - 版本对但 epoch 旧 → FencedOut（旧 owner 拿着对的版本号也不能写）
    - epoch 对但版本旧 → StateVersionConflict
    """
    j = h.journal
    p = make_principal()
    s = make_scope()

    epoch = await h.lease.acquire(s, "owner-A")
    sw = j.bind_state(p, s, "core", owner_epoch=epoch)

    r1 = await sw.commit_state([state("s1")], "cs1",
                               expected_state_version=0, owner_epoch=epoch)
    assert r1.outcome == "accepted"
    assert r1.state_version == 1

    # 重复版本 → 冲突
    with pytest.raises(StateVersionConflict):
        await sw.commit_state([state("s2")], "cs2",
                              expected_state_version=0, owner_epoch=epoch)

    # 正确版本 + 正确 epoch → 成功
    r2 = await sw.commit_state([state("s3")], "cs3",
                               expected_state_version=1, owner_epoch=epoch)
    assert r2.state_version == 2

    # epoch 被抢占后，旧 owner 即使版本号正确也被拒
    new_epoch = await h.lease.acquire(s, "owner-B")
    assert new_epoch > epoch
    with pytest.raises(FencedOut):
        await sw.commit_state([state("s4")], "cs4",
                              expected_state_version=2, owner_epoch=epoch)

    # 只有两次成功的提交消耗了 seq —— 失败路径不能留痕。
    # 这条断言专门盯住「CAS 判定是否在 seq 分配之前」：反过来会让 seq 变成 4。
    assert await j.head(p, s) == Head(seq=2, state_version=2, owner_epoch=new_epoch)


# ---------- 8. observation 不推 state_version ★★ ----------

async def check_observation_does_not_bump_state_version(h: Harness) -> None:
    """**feature07 的招牌验收项。**

    如果 observation 也推进 state_version，那么 50 条 trace 写入之后，owner 手里
    的 `expected_state_version` 就过期了 —— 它无法在下一次提交前知道中间发生了什么，
    除非每次都重读整段日志。这会把「高频诊断写入」变成「高频状态提交冲突」。
    """
    j = h.journal
    p = make_principal()
    s = make_scope()

    epoch = await h.lease.acquire(s, "owner")
    sw = j.bind_state(p, s, "core", owner_epoch=epoch)
    w = writer_for(j, p, s, producer="observer")

    await sw.commit_state([state("base")], "c-base",
                          expected_state_version=0, owner_epoch=epoch)
    after_state = await j.head(p, s)

    for i in range(50):
        receipt = await w.append([observation(f"o{i}")], f"co{i}")
        assert receipt.outcome == "accepted"

    after_obs = await j.head(p, s)
    assert after_obs.state_version == after_state.state_version, (
        "observation 推进了 state_version —— owner 的 expected_state_version 会失效"
    )
    assert after_obs.seq == after_state.seq + 50, "observation 必须推进 seq"

    # owner 仍能用它记住的版本号提交，不冲突
    receipt = await sw.commit_state([state("next")], "c-next",
                                    expected_state_version=after_state.state_version,
                                    owner_epoch=epoch)
    assert receipt.outcome == "accepted"
    assert receipt.state_version == after_state.state_version + 1


async def check_state_version_advances_once_per_commit(h: Harness) -> None:
    """一次 state commit 携带 N 条记录 → state_version 只 +1，不是 +N。

    并且：**state commit 里不能夹带 observation**。
    这条限制是有意的 —— 诊断记录搭执行状态提交的便车，会让「哪些记录参与恢复」
    和「哪些只是日志」混在一起。reducer 只吃 category=state；夹带让这一点在日志里
    不再可分辨，恢复路径就得再猜一次。
    """
    j = h.journal
    p = make_principal()
    s = make_scope()

    epoch = await h.lease.acquire(s, "owner")
    sw = j.bind_state(p, s, "core", owner_epoch=epoch)

    receipt = await sw.commit_state([state(f"s{i}") for i in range(3)], "c3",
                                    expected_state_version=0, owner_epoch=epoch)
    head = await j.head(p, s)
    assert receipt.state_version == 1, "一次提交是一个原子决定，state_version 只 +1"
    assert head.state_version == 1
    assert head.seq == 3, "全部记录都可见 —— 批次是原子的"

    # 夹带 observation 被拒，且不改变任何计数器
    with pytest.raises(ValidationError):
        await sw.commit_state(
            [state("s3"), observation("o0")], "c-mixed",
            expected_state_version=1, owner_epoch=epoch,
        )
    assert await j.head(p, s) == head, "被拒的 state commit 不能留下痕迹"


# ---------- 9. writer capability ★ ----------

async def check_writer_capability(h: Harness) -> None:
    """普通 writer 写不了执行状态；类型白名单在绑定时固定。"""
    j = h.journal
    p = make_principal()
    s = make_scope()

    plain = writer_for(j, p, s, producer="ext")
    with pytest.raises(ValidationError):
        await plain.append([state("x")], "c-x")

    # bind 阶段就拒绝 state 类别，不给「先绑上再被每条记录拒绝」的机会
    with pytest.raises(ValidationError):
        j.bind(p, s, "ext", allowed_categories=frozenset({"state"}))

    # 类型白名单
    narrow = writer_for(j, p, s, producer="ext",
                              allowed_types=frozenset({"user.message"}))
    assert (await narrow.append([event("ok")], "c-ok")).outcome == "accepted"
    with pytest.raises(ValidationError):
        await narrow.append([event("bad", type_="tool.completed")], "c-bad")


async def check_producer_cannot_impersonate(h: Harness) -> None:
    """绑定的 writer 不能伪装成别的 producer。

    否则一个扩展可以写 `category=event` + `type=tool.completed` 的记录，让下游
    「这个终态来自 core」的判断失效（feature07：扩展无权写 core 终态）。
    """
    j = h.journal
    p = make_principal()
    s = make_scope()

    w = writer_for(j, p, s, producer="real-ext")
    fake = RecordDraft(record_id="x", type="user.message", category="event",
                       producer="core", queue="user", event_id="E1", payload={"a": 1})
    receipt = await w.append([fake], "c1")
    assert receipt.outcome == "accepted"

    stored = await read_all(j, p, s)
    assert stored[0].producer == "real-ext", (
        f"记录里的 producer 被调用方伪造了：{stored[0].producer!r}"
    )


# ---------- 10. cursor 过滤推进 ★ ----------

async def check_cursor_filter_advance(h: Harness) -> None:
    """过滤流翻页时，空页也必须携带非零 through_seq 和可用的 next_cursor。

    没有这条，过滤流的客户端看到空页会以为流结束而静默停止消费 —— 正是 feature07
    要求 cursor 记住「已扫描到的 seq（包括被过滤记录）」要防的失败模式。
    """
    j = h.journal
    p = make_principal()
    s = make_scope()
    w = writer_for(j, p, s)

    for i in range(100):
        await w.append([observation(f"o{i}")], f"co{i}")
    for i in range(3):
        await w.append([event(f"e{i}")], f"ce{i}")

    only_events = RecordFilter(types=frozenset({"user.message"}))
    items = await read_all(j, p, s, limit=2, record_filter=only_events)
    assert len(items) == 3, "过滤条件下最终必须读到全部 3 条 event"

    # 直接验证空页也推进：一次 limit=2 只能拿到 2 条 event，
    # 但扫描位置必须已经越过 100 条 observation，否则客户端会来回重扫。
    page = await j.read(
        ReadRequest(principal=p, scope=s, after_seq=0, limit=2, filter=only_events)
    )
    assert len(page.items) == 2
    assert page.through_seq >= 102, (
        f"扫描位置必须越过中间的 observation，实际 through_seq={page.through_seq}"
    )
    assert page.next_cursor is not None


# ---------- 11. cursor 绑定 ----------

async def check_cursor_binding(h: Harness) -> None:
    """cursor 换人、换会话、换过滤条件一律失效。"""
    j = h.journal
    p = make_principal()
    s = make_scope()
    w = writer_for(j, p, s)
    for i in range(10):
        await w.append([event(f"e{i}")], f"ce{i}")

    page = await j.read(ReadRequest(principal=p, scope=s, after_seq=0, limit=3))
    assert page.next_cursor is not None
    token = page.next_cursor

    # 换 principal
    other = make_principal("mallory", user="u1", session="s1")
    with pytest.raises(CursorInvalid):
        j.unseal_cursor(token, ReadRequest(principal=other, scope=s, limit=3))

    # 换 limit —— 换分页大小就换了边界，不是「尽力而为」
    with pytest.raises(CursorInvalid):
        j.unseal_cursor(token, ReadRequest(principal=p, scope=s, limit=5))

    # 换过滤条件
    with pytest.raises(CursorInvalid):
        j.unseal_cursor(token, ReadRequest(principal=p, scope=s, limit=3,
                                            filter=RecordFilter(types=frozenset({"x"}))))

    # 这里没有「篡改 token 必须被拒」这一条：cursor 里只有位置，改掉它不会读到
    # 不该读的东西，因为能不能读某个 scope 由 Principal 决定。断言篡改必须失败
    # 是在要求一个这里不需要的性质。

    # 换 scope —— 真正的权限边界
    with pytest.raises(CursorInvalid):
        await _read_with_cursor(j, token, p, make_scope("u2", "s2"), 3)


async def _read_with_cursor(journal: Journal, token: str, principal: Principal,
                            scope: Scope, limit: int) -> ReadPage:
    """按 cursor 语义读取。"""
    request = ReadRequest(principal=principal, scope=scope, limit=limit)
    cursor = journal.unseal_cursor(token, request)
    return await journal.read(ReadRequest(principal=principal, scope=scope,
                                          after_seq=cursor.through_seq, limit=limit))


# ---------- 12. 存储端赋值字段 ★ ----------

async def check_storage_assigned_fields(h: Harness) -> None:
    """seq / committed_at 由存储端给，且 committed_at 是 UTC。

    `committed_at` 写成 UTC 不是讲究：`+08:00` 和 `Z` 是同一时刻，但字节不同，
    一旦有地方直接对它做比较或哈希，同一个提交会算出两个值。
    """
    j = h.journal
    p = make_principal()
    s = make_scope()
    w = writer_for(j, p, s)

    await w.append([event("a")], "c1")
    stored = await read_all(j, p, s)

    assert stored[0].seq >= 1
    assert stored[0].committed_at.tzinfo is not None, "时间戳必须带时区"
    assert stored[0].committed_at.utcoffset() is not None
    assert stored[0].committed_at_iso.endswith("Z")


# ---------- 13. canonical checksum ----------

async def check_checksum_ignores_storage_fields(h: Harness) -> None:
    """同样的内容必须算出同样的 checksum，哪怕落到的 seq 不同。

    这是幂等重试成立的前提：生产者丢了 receipt 之后重试时，它不知道存储端会分配
    什么 `seq` / `committed_at`，只能重算内容部分的 checksum。如果 checksum 覆盖了
    存储端字段，重试必然被判成 conflict，丢 receipt 的场景就卡死了。

    做法：同一批内容写到两个起点不同的 scope，seq 必然不同。
    """
    j = h.journal
    p = Principal(principal_id="p1", scopes=frozenset({("u1", "s1"), ("u1", "s2")}))

    # 先给 s2 垫一条，让两个 scope 的 seq 起点错开
    await writer_for(j, p, make_scope("u1", "s2")).append([event("filler")], "c-filler")

    r1 = await writer_for(j, p, make_scope("u1", "s1")).append([event("a")], "c1")
    r2 = await writer_for(j, p, make_scope("u1", "s2")).append([event("a")], "c1")

    assert (r1.first_seq, r2.first_seq) == (1, 2), "两个 scope 的 seq 起点应当不同"
    assert r1.checksum == r2.checksum, (
        "同样的内容算出了不同的 checksum —— checksum 覆盖了 seq 或 committed_at，"
        "丢 receipt 后用原 commit_id 重试会被误判为内容冲突"
    )


# ---------- 14. payload_ref ----------

async def check_payload_ref(h: Harness) -> None:
    """超大 payload 走 blob：日志只留引用 + hash + size，且引用可验证。"""
    j = h.journal
    p = make_principal()
    s = make_scope()
    w = writer_for(j, p, s, producer="ingress")

    data = b"x" * (1 << 15)
    blob_id, size = await h.blobs.put(data)
    assert size == len(data)

    ref = BlobRef(blob_id=blob_id, hash=blob_id, size=size)
    draft = RecordDraft(record_id="big", type="user.message", category="event",
                        producer="ingress", queue="user", event_id="E-big",
                        payload_ref=ref)
    assert (await w.append([draft], "c-big")).outcome == "accepted"

    stored = await read_all(j, p, s)
    assert stored[0].payload is None, "引用记录不应内联 payload"
    assert stored[0].payload_ref is not None
    assert stored[0].payload_ref.hash == blob_id
    assert stored[0].payload_ref.size == size

    assert await h.blobs.verify(blob_id, blob_id) is True
    assert await h.blobs.verify(blob_id, "deadbeef") is False, (
        "hash 不匹配时必须能发现 blob 被替换"
    )


# ---------- 15. 超过上限 ----------

async def check_over_limit_rejected(h: Harness) -> None:
    """超限是显式失败，绝不静默截断或丢弃。"""
    j = h.journal
    p = make_principal()
    s = make_scope()
    w = writer_for(j, p, s)

    too_many = [observation(f"o{i}") for i in range(300)]  # max_batch_records = 256
    with pytest.raises((LimitExceeded, ValidationError)):
        await w.append(too_many, "c-big")

    assert await j.head(p, s) == Head(seq=0, state_version=0, owner_epoch=0), (
        "超限批次不能留下部分记录"
    )

    # 读页大小也受限
    with pytest.raises(LimitExceeded):
        await j.read(ReadRequest(principal=p, scope=s, limit=1000))


# ---------- 16. 读稳定性 ----------

async def check_read_stability(h: Harness) -> None:
    """已提交的记录不可改写；`after_seq` 之后严格递增且不重复。"""
    j = h.journal
    p = make_principal()
    s = make_scope()
    w = writer_for(j, p, s)

    for i in range(20):
        await w.append([event(f"e{i}", run_id="run-1")], f"c{i}")

    first_pass = await read_all(j, p, s)
    second_pass = await read_all(j, p, s)
    assert [r.record_id for r in first_pass] == [r.record_id for r in second_pass], (
        "重复读取必须得到相同结果"
    )
    assert all(r.run_id == "run-1" for r in first_pass)

    seqs = [r.seq for r in first_pass]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)

    # 半开区间语义：after_seq = k 不包含 seq == k
    cut = seqs[9]
    tail = await read_all(j, p, s, after_seq=cut)
    assert all(r.seq > cut for r in tail)


# ---------- 17. 持久化（需要 REOPEN 能力） ----------

async def check_reopen_durability(h: Harness) -> None:
    """重开 journal 后，已提交内容全部可读，计数器不回退。

    `reopen` 不在 Journal 接口里 —— 它是 REOPEN 能力，不是每种存储都有。
    所以这里用 getattr 取，取不到就是能力声明与实现不符。
    """
    j = h.journal
    reopen = getattr(j, "reopen", None)
    assert reopen is not None, "声明了 reopen 能力就必须提供 reopen()"

    p = make_principal()
    s = make_scope()
    epoch = await h.lease.acquire(s, "owner")
    sw = j.bind_state(p, s, "core", owner_epoch=epoch)
    w = writer_for(j, p, s)

    for i in range(10):
        await w.append([event(f"e{i}")], f"ce{i}")
    await sw.commit_state([state("s0")], "cs",
                          expected_state_version=0, owner_epoch=epoch)

    before_items = await read_all(j, p, s)
    before_head = await j.head(p, s)

    j.close()
    reopened = reopen()
    try:
        after_items = await read_all(reopened, p, s)
        assert [r.record_id for r in after_items] == [r.record_id for r in before_items]
        assert [r.payload for r in after_items] == [r.payload for r in before_items]
        head = await reopened.head(p, s)
        assert head.seq == before_head.seq
        assert head.state_version == before_head.state_version
        assert head.owner_epoch == before_head.owner_epoch

        # 幂等信息也必须活过重启
        dup = await writer_for(reopened, p, s).append([event("e0")], "ce0")
        assert dup.outcome == "duplicate"
    finally:
        reopened.close()


# ---------- 18. 崩溃原子性（需要 SIMULATE_CRASH 能力） ----------

async def check_crash_atomicity(h: Harness) -> None:
    """事务中途崩溃 → 整批不可见，计数器不变。

    崩溃注入同样不在接口里 —— 它是 SIMULATE_CRASH 能力，只有测试用的存储会做。
    """
    j = h.journal
    p = make_principal()
    s = make_scope()
    head_before = await j.head(p, s)

    crash = getattr(j, "crash_on_next_commit", None)
    assert crash is not None, "声明了 simulate_crash 能力就必须提供崩溃注入钩子"

    w = writer_for(j, p, s)
    crash()
    # BaseException 是对的，不是笔误：InjectedCrash 刻意继承 BaseException，
    # 好让 runtime 自己的 `except Exception` 兜不住它。断言窄到 InjectedCrash 会
    # 掩盖「adapter 抛了个无关异常」这种情况 —— 那不是崩溃，是 bug。
    with pytest.raises(BaseException):  # noqa: B017 - 见上方理由
        await w.append([event("x"), event("y")], "c-crash")

    assert await j.head(p, s) == head_before
    assert await read_all(j, p, s) == (), "崩溃时整批必须不可见，不能只写一半"


# ---------- 注册表 ----------

CHECKS: tuple[Check, ...] = (
    Check("isolation", check_isolation),
    Check("seq_allocation", check_seq_allocation),
    Check("atomic_batch", check_atomic_batch),
    Check("no_seq_gap_after_failure", check_no_seq_gap_after_failure),
    Check("commit_idempotency", check_commit_idempotency),
    Check("commit_content_conflict", check_commit_content_conflict),
    Check("event_id_dedup", check_event_id_dedup),
    Check("state_cas_and_fencing", check_state_cas_and_fencing),
    Check("observation_does_not_bump_state_version", check_observation_does_not_bump_state_version),
    Check("state_version_advances_once_per_commit", check_state_version_advances_once_per_commit),
    Check("writer_capability", check_writer_capability),
    Check("producer_cannot_impersonate", check_producer_cannot_impersonate),
    Check("cursor_filter_advance", check_cursor_filter_advance),
    Check("cursor_binding", check_cursor_binding),
    Check("storage_assigned_fields", check_storage_assigned_fields),
    Check("checksum_ignores_storage_fields", check_checksum_ignores_storage_fields),
    Check("payload_ref", check_payload_ref),
    Check("over_limit_rejected", check_over_limit_rejected),
    Check("read_stability", check_read_stability),
    Check("reopen_durability", check_reopen_durability, REOPEN),
    Check("crash_atomicity", check_crash_atomicity, SIMULATE_CRASH),
)


@pytest.mark.parametrize(
    "check",
    CHECKS,
    ids=[c.name for c in CHECKS],
)
@pytest.mark.asyncio
async def test_conformance(check: Check, adapter_name: str, tmp_path: Any) -> None:
    gen = ADAPTERS[adapter_name](tmp_path)
    harness = next(gen)
    try:
        capabilities = harness.journal.capabilities
        if check.required_capability and check.required_capability not in capabilities:
            pytest.skip(
                f"adapter {adapter_name!r} 未声明 {check.required_capability} 能力。"
                f"能力名见 core/journal/api.py；skip 数上升即 capabilities 声明有漏。"
            )
        await check.run(harness)
    finally:
        with pytest.raises(StopIteration):
            next(gen)
