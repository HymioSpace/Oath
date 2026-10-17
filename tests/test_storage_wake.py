"""wake 的版本计数与 brain 的认领。

`requested / claimed / completed` 三个数解决的是「唤醒会不会丢」：入队期间来的
唤醒记在 requested 上，brain 认领时把 requested 拍到 claimed，跑完把 completed
追平。下一次入队从头再开始计数，所以一次漏掉的唤醒会让这个用户多跑一轮，
而不是静默停在没有输入的状态。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.protocol.interface.storage import LeaseLost
from core.storage.database import encode
from tests._support.storage import Storage


def _counters(service: Storage, user_id: str = "u1") -> dict[str, Any]:
    with service.transaction() as db:
        row = db.execute(
            "SELECT requested,claimed,completed FROM scheduling WHERE user_id=?", (user_id,)
        ).fetchone()
        return dict(row)


def _fail_the_active_step(service: Storage, user_id: str = "u1") -> None:
    """把当前 step 置为 failed，用来验证 wake 的那道闸。"""
    state = service.call("state.read", user_id=user_id)["state"]
    state["steps"] = {"s1": {"status": "failed"}}
    state["meta"] = {"active_step": "s1"}
    with service.transaction() as db:
        db.execute(
            "UPDATE users SET state_json=? WHERE user_id=?",
            (encode(state), user_id),
        )


def test_a_wake_is_a_version_bump_not_a_flag(service: Storage) -> None:
    """第一次唤醒：requested 0→1。"""
    service.call("wake", user_id="u1", force=True)

    assert _counters(service) == {"requested": 1, "claimed": 0, "completed": 0}


def test_a_wake_with_nothing_to_process_does_nothing(service: Storage) -> None:
    """没有 inbox 输入的唤醒不计数 —— 否则用户会为一个空跑轮占住 brain。"""
    service.call("wake", user_id="u1")

    assert _counters(service)["requested"] == 0
    assert service.call("wake", user_id="u1", force=True) is None
    assert _counters(service)["requested"] == 1


def test_a_wake_during_a_run_is_not_lost(service: Storage) -> None:
    """这一版正在跑（requested==claimed），新来的唤醒推到下一版，不会被本版吞掉。"""
    service.call("wake", user_id="u1", force=True)
    assert service.call("brain.claim", owner="w", ttl=60) is not None
    assert _counters(service)["claimed"] == 1

    service.call("wake", user_id="u1", force=True)

    assert _counters(service)["requested"] == 2


def test_finishing_catches_completed_up_to_what_was_claimed(service: Storage) -> None:
    """跑完把 completed 追到 claimed。requested 还比它大，说明还有下一轮。"""
    service.call("wake", user_id="u1", force=True)
    fence = service.call("brain.claim", owner="w", ttl=60)
    service.call("wake", user_id="u1", force=True)
    service.call("brain.finish", user_id="u1", fence=fence)

    assert _counters(service) == {"requested": 2, "claimed": 1, "completed": 1}


def test_a_retry_is_backed_off_before_the_next_claim(service: Storage) -> None:
    """重试等一秒再认领 —— 立刻重跑一个刚失败的用户就是忙循环。"""
    service.call("wake", user_id="u1", force=True)
    fence = service.call("brain.claim", owner="w", ttl=60)
    service.call("brain.finish", user_id="u1", fence=fence, retry=True)

    assert _counters(service)["requested"] == 2
    assert service.call("brain.claim", owner="w", ttl=60) is None


def test_a_backed_off_retry_becomes_claimable_when_its_time_comes(service: Storage) -> None:
    service.call("wake", user_id="u1", force=True)
    fence = service.call("brain.claim", owner="w", ttl=60)
    service.call("brain.finish", user_id="u1", fence=fence, retry=True)
    with service.transaction() as db:
        db.execute("UPDATE scheduling SET ready_at=0 WHERE user_id='u1'")

    assert service.call("brain.claim", owner="w", ttl=60) is not None


def test_a_claim_only_returns_a_user_with_pending_work(service: Storage) -> None:
    assert service.call("brain.claim", owner="w", ttl=60) is None


def test_a_user_whose_brain_is_held_is_skipped(service: Storage) -> None:
    """u1 已被某个 brain 占住，u2 该被服务，而不是整个 brain 空转等 u1。"""
    service.call("wake", user_id="u1", force=True)
    service.call("wake", user_id="u2", force=True)
    assert service.call("brain.claim", owner="w1", ttl=60)["user_id"] == "u1"

    assert service.call("brain.claim", owner="w2", ttl=60)["user_id"] == "u2"


def test_finishing_releases_the_brain_lane(service: Storage) -> None:
    """不释放的话，用户下次永远轮不到 —— 这次跑挂了，下一次也起不来。"""
    service.call("wake", user_id="u1", force=True)
    fence = service.call("brain.claim", owner="w1", ttl=60)
    service.call("brain.finish", user_id="u1", fence=fence)
    service.call("wake", user_id="u1", force=True)

    assert service.call("brain.claim", owner="w2", ttl=60) is not None


def test_finishing_with_someone_elses_fence_is_refused(service: Storage) -> None:
    """旧 brain 在租约被抢之后回来收尾，不能把新一版的 completed 抹平。"""
    service.call("wake", user_id="u1", force=True)
    stale = service.call("brain.claim", owner="w1", ttl=-1)
    service.call("brain.claim", owner="w2", ttl=60)

    with pytest.raises(LeaseLost):
        service.call("brain.finish", user_id="u1", fence=stale)


def test_a_wake_on_a_paused_step_is_held_back(service: Storage) -> None:
    """暂停中的 step 不该被新唤醒带起来 —— 那会让用户看到一串没跑完的输出。"""
    _fail_the_active_step(service)

    service.call("wake", user_id="u1", force=True)

    assert _counters(service)["requested"] == 0


def test_a_delayed_wake_is_not_claimable_yet(service: Storage) -> None:
    """`system.invoke` 有 delay 时，brain 在到点之前拿不到它。"""
    service.call("wake", user_id="u1", force=True)
    with service.transaction() as db:
        db.execute("UPDATE scheduling SET ready_at=? WHERE user_id='u1'", (2**31,))

    assert service.call("brain.claim", owner="w", ttl=60) is None


def test_a_claim_picks_the_user_that_has_waited_longest(service: Storage) -> None:
    """先到先得。requested 同时是 1 时按 ready_at 排。"""
    service.call("wake", user_id="u2", force=True)
    service.call("wake", user_id="u1", force=True)
    with service.transaction() as db:
        db.execute("UPDATE scheduling SET ready_at=1000 WHERE user_id='u2'")
        db.execute("UPDATE scheduling SET ready_at=1 WHERE user_id='u1'")

    assert service.call("brain.claim", owner="w", ttl=60)["user_id"] == "u1"
