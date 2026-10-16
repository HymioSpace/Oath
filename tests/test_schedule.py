"""周期任务：每项最多一个在途轮次。"""

from __future__ import annotations

import threading
import time

from core.supervisor.schedule import Periodic


def test_a_round_that_overruns_does_not_start_another() -> None:
    """上一轮没收手就不再起一轮 —— 两轮同时跑就不再是「每项最多一个在途轮次」。"""
    release = threading.Event()
    entered = threading.Event()
    rounds = 0

    def work(_deadline: float) -> None:
        nonlocal rounds
        rounds += 1
        entered.set()
        release.wait(10)

    task = Periodic("probe", interval=0.05, timeout=0.05, work=work)
    task.start()
    try:
        assert entered.wait(5)
        time.sleep(0.5)  # 好几轮的时间。
        assert rounds == 1
    finally:
        release.set()
        task.stop()
    assert rounds == 1


def test_the_next_round_runs_once_the_last_one_ends() -> None:
    """跳过一轮不是取消这项任务：卡过的那轮结束后照常继续。"""
    rounds = 0
    slow = threading.Event()

    def work(_deadline: float) -> None:
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            slow.wait(0.3)

    task = Periodic("probe", interval=0.05, timeout=0.05, work=work)
    task.start()
    try:
        deadline = time.monotonic() + 5
        while rounds < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        slow.set()
        task.stop()
    assert rounds == 2


def test_a_failing_round_backs_off_but_still_runs() -> None:
    """一轮抛错只推后下一轮，不让这项任务就此停下。"""
    rounds: list[float] = []

    def work(_deadline: float) -> None:
        rounds.append(time.monotonic())
        raise ValueError("本轮失败")

    task = Periodic("probe", interval=0.05, timeout=0.05, work=work)
    task.start()
    try:
        deadline = time.monotonic() + 5
        while len(rounds) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        task.stop()
    assert len(rounds) == 2
