"""真实进程崩溃测试（slow）。

验证的是**另一件事**，不是 `test_journal_durability.py` 里的半行容忍：
这里打的是真进程，验证「`append` 返回成功后，记录在进程被 SIGKILL 之后
仍然读得回来」。

半行容忍在本地复现不出来 —— 普通文件系统上 `SIGKILL` 杀不掉页缓存里的
数据，`os.write()` 返回后内容已经在页缓存里。所以那边用 `truncate()` 人工
构造，见该文件的说明。
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.journal.impl import FileJournal
from core.protocol.datatype import Cursor, CursorKind, EventType, Scope

WRITER = Path(__file__).parent / "_fixtures" / "crash_writer.py"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _spawn(
    root: Path, ready: Path, session_id: str = "s1", limit: int | None = None
) -> subprocess.Popen[str]:
    argv = [sys.executable, str(WRITER), str(root), "u1", session_id, str(ready)]
    if limit is not None:
        argv.append(str(limit))
    return subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=REPO_ROOT
    )


def _wait_until_writing(ready: Path, timeout: float = 20.0) -> None:
    """等到子进程确认写成功至少一条。

    不等这一步的话，「杀得太早」和「真的坏了」在断言里长得一模一样，
    测试就只是在测机器快慢。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"子进程 {timeout}s 内没有成功写入任何记录")


def _acks(stdout: str) -> list[int]:
    """子进程报告的、已拿到成功返回的游标。"""
    return [int(line.split("\t")[1]) for line in stdout.splitlines() if "\t" in line]


@pytest.mark.slow
def test_acknowledged_appends_survive_sigkill(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    ready = tmp_path / "ready"
    scope = Scope(user_id="u1", session_id="s1")

    proc = _spawn(root, ready)
    _wait_until_writing(ready)
    time.sleep(0.3)  # 再写几条，确保不在第一条上就杀掉
    proc.send_signal(signal.SIGKILL)
    stdout, stderr = proc.communicate(timeout=30)
    assert proc.returncode == -signal.SIGKILL, f"没被 SIGKILL 杀死: {stderr}"

    acks = _acks(stdout)
    assert len(acks) > 1, "只写了一条就杀，测不出东西"

    with FileJournal(root) as journal:
        recovery = journal.recover(scope)
        records = list(journal.read_range(scope, Cursor(CursorKind.LINE, 0)))

    # append 返回成功 = 已持久化。进程被 kill 不该让这些记录消失。
    assert {r.cursor.value for r in records} >= set(acks)
    # 每一条读回来都必须结构完整 —— 没有半行被当成记录。
    assert len(recovery.pending) == len(records)


@pytest.mark.slow
def test_journal_reopens_cleanly_after_sigkill(tmp_path: Path) -> None:
    """连着崩两次，每次之后重新打开都不能抛错。"""
    root = tmp_path / "journal"
    ready = tmp_path / "ready"
    scope = Scope(user_id="u1", session_id="s1")

    for round_no in range(2):
        round_ready = ready.with_name(f"ready{round_no}")
        proc = _spawn(root, round_ready)
        _wait_until_writing(round_ready)
        time.sleep(0.3)
        proc.send_signal(signal.SIGKILL)
        proc.communicate(timeout=30)

        with FileJournal(root) as journal:
            journal.recover(scope)  # 不抛错即通过

    # 两轮崩溃之后仍能重放，且幂等索引与磁盘一致 —— 索引是打开时重建的，
    # 重建结果必须和崩溃前一致。
    with FileJournal(root) as journal:
        before = len(list(journal.read_range(scope, Cursor(CursorKind.LINE, 0))))
        replay = journal.append(
            scope,
            EventType.USER_MESSAGE,
            # 与 crash_writer 写的第一条内容一致，才能命中幂等。
            {"content": f"0:{'x' * 4096}"},
            payload_schema_version=1,
            idempotency_key="k0",
        )
        after = len(list(journal.read_range(scope, Cursor(CursorKind.LINE, 0))))

    assert replay.idempotent_hit is True, "重启后幂等失效，重放会写入重复记录"
    assert after == before


@pytest.mark.slow
def test_concurrent_writers_to_different_scopes_stay_isolated(tmp_path: Path) -> None:
    """不同 scope 并发写 —— 分文件布局的直接后果。"""
    root = tmp_path / "journal"

    procs = [_spawn(root, tmp_path / f"ready{i}", session_id=f"s{i}", limit=20) for i in range(2)]
    for proc in procs:
        assert proc.wait(timeout=30) == 0

    with FileJournal(root) as journal:
        a = journal.recover(Scope(user_id="u1", session_id="s0"))
        b = journal.recover(Scope(user_id="u1", session_id="s1"))
    # 两个进程写各自的 scope，互不干扰 —— 这正是按 scope 分目录换来的。
    assert len(a.pending) == 20
    assert len(b.pending) == 20
    assert [str(r.payload["content"]).split(":")[0] for r in a.pending] == [
        str(i) for i in range(20)
    ]
