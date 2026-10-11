"""JSONL journal 的持久性测试 —— 用真实子进程验证进程被杀以后日志还在。

conformance 套件里的 `crash_atomicity` 是在进程内注入的，它只证明代码路径能处理
半行，证明不了「进程真的死了以后日志还在」。这里用真实子进程补上。
"""
from __future__ import annotations

import json
import pathlib
import signal
import subprocess
import sys
import time
from typing import Any

import pytest

from core.errors import LogCorrupt
from core.journal import (
    JsonlJournal,
    Principal,
    ReadRequest,
    RecordDraft,
    RecordFilter,
    Scope,
)
from tests._support.journal_factory import make_principal

CHILD = pathlib.Path(__file__).parent / "_fixtures" / "crash_child.py"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
USER = "u1"
SESSION = "s1"
N_COMMITS = 25


def read_acks(path: pathlib.Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def wait_for_acks(path: pathlib.Path, count: int, timeout: float = 60.0) -> list[str]:
    """等到子进程 ack 了至少 count 条，或超时。

    轮询而不是固定 sleep：固定 sleep 会在慢机器上偶发失败，而偶发失败的测试
    等于没有测试。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        acks = read_acks(path)
        if len(acks) >= count:
            return acks
        time.sleep(0.02)
    raise AssertionError(f"等待 {count} 条 ack 超时，实际 {len(read_acks(path))} 条")


def spawn_child(root: pathlib.Path, acks: pathlib.Path, n: int,
                mode: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, str(CHILD), str(root), USER, SESSION, str(acks), str(n), mode],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def kill_and_wait(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        child.kill()
    child.wait(timeout=30)


def journal_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(root.rglob("journal.jsonl"))


def event_draft(record_id: str) -> RecordDraft:
    return RecordDraft(
        record_id=record_id, type="user.message", category="event", producer="core",
        queue="user", event_id=record_id, payload={"id": record_id},
    )


async def read_all(journal: JsonlJournal, principal: Principal,
                   scope: Scope) -> tuple[Any, ...]:
    items: list[Any] = []
    after = 0
    while True:
        page = await journal.read(
            ReadRequest(principal=principal, scope=scope, after_seq=after,
                        limit=100, filter=RecordFilter())
        )
        items.extend(page.items)
        if page.next_cursor is None:
            return tuple(items)
        after = page.through_seq


# ---------- 1. 进程被 SIGKILL ----------

async def test_sigkill_preserves_acked_commits(tmp_path: pathlib.Path) -> None:
    """SIGKILL 之后，每个拿到过 receipt 的记录都还在，且 seq 连续。

    已确认的写入不会因为进程死掉而消失；未确认的写入允许消失。
    """
    root = tmp_path / "journal"
    acks = tmp_path / "acked.txt"
    child = spawn_child(root, acks, N_COMMITS, "wait_for_sigkill")
    try:
        confirmed = wait_for_acks(acks, N_COMMITS)
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=30)
    finally:
        kill_and_wait(child)

    scope = Scope(user_id=USER, session_id=SESSION)
    journal = JsonlJournal(root=root)
    try:
        items = await read_all(journal, make_principal(unrestricted=True), scope)
        present = {r.record_id for r in items}

        missing = [tag for tag in confirmed if tag not in present]
        assert not missing, f"已 ack 的记录在 SIGKILL 后丢失: {missing}"

        assert [r.seq for r in items] == list(range(1, len(items) + 1)), (
            "seq 必须连续 —— 崩溃不能留下空洞"
        )
        head = await journal.head(make_principal(unrestricted=True), scope)
        assert head.seq == len(items)
    finally:
        journal.close()


# ---------- 2. 最后一行只写了一半 ----------

async def test_half_written_last_line_is_dropped_on_reopen(tmp_path: pathlib.Path) -> None:
    """写半行后进程死掉：重开时截掉它，日志还能继续追加。

    只写了一半的那行不算一次提交。否则恢复时会多出一条本不存在的记录。
    """
    root = tmp_path / "journal"
    child = spawn_child(root, tmp_path / "acked.txt", 5, "self_half_line")
    child.wait(timeout=60)
    assert child.returncode == 9, "子进程应走到 self_half_line 分支"

    scope = Scope(user_id=USER, session_id=SESSION)
    principal = make_principal(unrestricted=True)

    # 先确认前提成立：文件末尾确实有一行解析不出来。不确认的话，这个测试
    # 什么都没证明 —— 它可能只是因为没有半行才通过。
    found_half_line = False
    for path in journal_files(root):
        raw = path.read_bytes()
        for line in raw.split(b"\n"):
            if line and not _parses(line):
                found_half_line = True
    assert found_half_line, "前提不成立：文件里没有半行，这个测试什么也没证明"

    journal = JsonlJournal(root=root)
    try:
        items = await read_all(journal, principal, scope)
        assert len(items) == 5 + 1, "5 次 append + 1 次 state commit"
        assert all(r.record_id != "halfwritten" for r in items), (
            "只写了一半的那次提交不能出现在日志里"
        )
        head_before = await journal.head(principal, scope)
    finally:
        journal.close()

    # 重开时截断已经发生：文件里现在每一条都是完整 JSON 行。
    for path in journal_files(root):
        for line in path.read_bytes().split(b"\n"):
            if line:
                assert _parses(line), f"重开之后半行还留在文件里: {path}"

    reopened = JsonlJournal(root=root)
    try:
        receipt = await reopened.bind(principal, scope, "core").append(
            [event_draft("after")], "c-after",
        )
        assert receipt.outcome == "accepted"
        items_after = await read_all(reopened, principal, scope)
        assert [r.record_id for r in items_after][-1] == "after"
        assert (await reopened.head(principal, scope)).seq == head_before.seq + 1
    finally:
        reopened.close()


# ---------- 3. 单写者 ----------

async def test_second_writer_is_rejected(tmp_path: pathlib.Path) -> None:
    """同一 scope 的第二个写者拿不到锁。

    单写者是正确性的前提：两个进程各自分配 seq 会立刻产生空洞和重复。
    """
    root = tmp_path / "journal"
    scope = Scope(user_id=USER, session_id=SESSION)
    principal = make_principal()

    first = JsonlJournal(root=root)
    try:
        assert (await first.bind(principal, scope, "core")
                .append([event_draft("a")], "c1")).outcome == "accepted"

        second = JsonlJournal(root=root)
        try:
            with pytest.raises(OSError):
                await second.bind(principal, scope, "core").append(
                    [event_draft("b")], "c2",
                )
        finally:
            second.close()
    finally:
        first.close()


# ---------- 4. 一批记录只写了一半 ----------

async def test_batch_cut_in_half_leaves_nothing_visible(tmp_path: pathlib.Path) -> None:
    """一批 3 条的提交写到一半死掉：整批不可见，不是前两条可见。

    这是「一行一次提交」真正要买的东西。如果实现是一行一条记录，写到一半就留下
    前两条，而恢复时没有任何东西能说明该留几条。
    """
    root = tmp_path / "journal"
    child = spawn_child(root, tmp_path / "acked.txt", 3, "half_batch")
    assert child.wait(timeout=60) == 9, "子进程应走到 half_batch 分支"

    # 前提必须先成立，否则这个测试什么都没证明。
    assert any(
        not _parses(line)
        for path in journal_files(root)
        for line in path.read_bytes().split(b"\n")
        if line
    ), "前提不成立：文件里没有半行"

    scope = Scope(user_id=USER, session_id=SESSION)
    journal = JsonlJournal(root=root)
    try:
        items = await read_all(journal, make_principal(unrestricted=True), scope)
        ids = {r.record_id for r in items}
        assert "c-halfwritten" not in {r.commit_id for r in items}, "半行提交被当成了提交"
        assert not {f"halfwritten{i}" for i in range(3)} & ids, (
            "这一批里已经落盘的前几条也不能被看见"
        )
        assert [r.seq for r in items] == list(range(1, len(items) + 1)), "seq 不能有空洞"
        assert (await journal.head(make_principal(unrestricted=True), scope)).seq == len(items)
    finally:
        journal.close()


# ---------- 5. 中间一行损坏 ----------

async def test_corrupt_middle_line_is_reported_not_truncated(
    tmp_path: pathlib.Path,
) -> None:
    """中间一整行解析不出来 → 报 LogCorrupt，且一个字都不许截掉。

    这条防的是静默丢数据：把「解析失败」一律当成「末尾那半行」，就会把损坏点之后
    还完好的提交一起 truncate 掉，而且没有任何声音。日志不可原位修改，丢掉的记录
    再也回不来。

    另外要求重复调用都报错 —— 只报一次就等于「重试一下就好了」，那还是静默。
    """
    root = tmp_path / "journal"
    scope = Scope(user_id=USER, session_id=SESSION)
    principal = make_principal(unrestricted=True)

    journal = JsonlJournal(root=root)
    try:
        writer = journal.bind(principal, scope, "core")
        for i in range(4):
            await writer.append([event_draft(f"r{i}")], f"c{i}")
    finally:
        journal.close()

    (path,) = journal_files(root)
    lines = path.read_bytes().split(b"\n")
    assert len(lines) == 5, "4 行提交 + 末尾空串"
    lines[2] = b'{"v":1,"kind":"commit","garbage":true' + b" " * 40
    path.write_bytes(b"\n".join(lines))
    size_before = path.stat().st_size

    reopened = JsonlJournal(root=root)
    try:
        for _ in range(2):
            with pytest.raises(LogCorrupt):
                await read_all(reopened, principal, scope)
            with pytest.raises(LogCorrupt):
                await reopened.head(principal, scope)
    finally:
        reopened.close()

    assert path.stat().st_size == size_before, "损坏点之后的字节被截掉了"
    # r2 那一行就是我们弄坏的那行，它没了是应该的。关键是 r3 —— 它排在损坏点
    # 后面，而「把解析失败当成末尾半行」的写法会把它一起截掉。
    raw = path.read_bytes()
    for record_id in (b'"r0"', b'"r1"', b'"r3"'):
        assert record_id in raw, f"{record_id!r} 被截掉了 —— 它本来还在文件里"


def _parses(line: bytes) -> bool:
    try:
        json.loads(line)
    except json.JSONDecodeError:
        return False
    return True