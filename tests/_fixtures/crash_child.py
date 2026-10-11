"""崩溃子进程：给 journal 的 durability 测试用。

两种死法，对应两种故障：

- `SIGKILL`（由父进程发起）：进程在两次 append 之间被杀。这是最真实的故障 ——
  没有机会清理、没有 atexit、没有 flush。
- `self_half_line`：故意只写半行就立刻 `_exit`。模拟写盘写到一半被打断，验证
  重开时那半行会被丢掉，而不是被当成一次提交。
- `half_batch`：同样只写半行，但这次提交的是一批 3 条记录，验证整批都不可见，
  而不是前两条可见。

用法（由 tests/test_journal_jsonl_durability.py 驱动，不要手工跑）::

    python crash_child.py <root> <user> <session> <ack_path> <n_commits> <mode>
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from core.clock import ManualClock
from core.journal import JsonlJournal, Principal, RecordDraft, Scope

FIXED = ManualClock()


def event(record_id: str) -> RecordDraft:
    return RecordDraft(
        record_id=record_id,
        type="user.message",
        category="event",
        producer="child",
        queue="user",
        event_id=record_id,
        payload={"id": record_id},
    )


async def main(root: str, user: str, session: str, ack_path: str,
               n_commits: int, mode: str) -> None:
    journal = JsonlJournal(root=root, clock=FIXED)
    scope = Scope(user_id=user, session_id=session)
    principal = Principal.unrestricted("child")
    writer = journal.bind(principal, scope, "child")
    lease = journal.lease()
    epoch = await lease.acquire(scope, "child")
    state_writer = journal.bind_state(principal, scope, "core", owner_epoch=epoch)

    # 刻意不关：这个进程要活到被 SIGKILL，ack 文件必须全程可写且逐条 fsync。
    ack_file = open(ack_path, "a", encoding="utf-8")  # noqa: SIM115

    def ack(tag: str) -> None:
        """记录一条「已经拿到 receipt」。每次都 fsync 是必须的：父进程据此判断
        哪些 commit 必须出现在日志里。没有它，测试只能证明日志里现在有什么，
        证明不了该有的没丢。"""
        ack_file.write(tag + "\n")
        ack_file.flush()
        os.fsync(ack_file.fileno())

    for i in range(n_commits):
        receipt = await writer.append([event(f"e{i}")], f"c{i}")
        assert receipt.outcome == "accepted", receipt
        ack(f"e{i}")
        if i % 7 == 0:
            await state_writer.commit_state(
                [RecordDraft(record_id=f"s{i}", type="session.state",
                             category="state", producer="core", payload={"i": i})],
                f"cs{i}", expected_state_version=i // 7, owner_epoch=epoch,
            )
            ack(f"s{i}")

    if mode in ("self_half_line", "half_batch"):
        # InjectedCrash 继承 BaseException，suppress 才是这里该用的宽度。
        batch = (
            [event(f"halfwritten{i}") for i in range(3)]
            if mode == "half_batch"
            else [event("halfwritten")]
        )
        journal.crash_on_next_commit()
        with contextlib.suppress(BaseException):
            await writer.append(batch, "c-halfwritten")
        os._exit(9)

    # mode == "wait_for_sigkill": 父进程会在这里把我们打死
    while True:
        await asyncio.sleep(0.01)


if __name__ == "__main__":
    if len(sys.argv) != 7:
        sys.exit(f"用法: {sys.argv[0]} <root> <user> <session> <ack> <n> <mode>")
    root, user, session, ack, n, mode = sys.argv[1:7]
    asyncio.run(main(root, user, session, ack, int(n), mode))