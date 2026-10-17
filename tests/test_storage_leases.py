"""lease 的排他性与 fencing token。

这些用例只碰 SQLite —— lease 是有期限的所有权，不依赖 RabbitMQ 或进程存活。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from core.protocol.interface.storage import LeaseLost
from core.storage import scheduling
from tests._support.storage import Storage


def _acquire(service: Storage, lane: str, owner: str, ttl: int = 60, user_id: str = "u1") -> Any:
    return service.call("lease.acquire", user_id=user_id, lane=lane, owner=owner, ttl=ttl)


def test_a_fresh_lease_is_handed_out(service: Storage) -> None:
    fence = _acquire(service, "consumer:user", "worker-a")

    assert fence == {"user_id": "u1", "lane": "consumer:user", "owner": "worker-a", "token": 1}


def test_a_live_lease_blocks_a_second_owner(service: Storage) -> None:
    """两个 consumer 同时醒着，就要有一个空转 —— 不能都以为自己独占。"""
    _acquire(service, "consumer:user", "worker-a")

    assert _acquire(service, "consumer:user", "worker-b") is None


def test_an_expired_lease_can_be_taken_over_and_the_token_moves_on(service: Storage) -> None:
    """token 递增是给下游的 fence：拿着旧 token 的写入会被认出来。"""
    stale = _acquire(service, "consumer:user", "worker-a", ttl=-1)
    taken = _acquire(service, "consumer:user", "worker-b")

    assert taken is not None
    assert taken["token"] == stale["token"] + 1
    assert taken["owner"] == "worker-b"


def test_a_replaced_owner_holds_a_fence_that_no_longer_writes(service: Storage) -> None:
    """这正是 fencing token 的用途：过期 owner 醒来继续写要被挡住。"""
    stale = _acquire(service, "consumer:user", "worker-a", ttl=-1)
    service.call("lease.acquire", user_id="u1", lane="consumer:user", owner="worker-b", ttl=60)

    with pytest.raises(LeaseLost):
        service.call("lease.release", user_id="u1", fence=stale, lane="consumer:user", ttl=60)


def test_a_lane_outside_the_closed_set_is_refused(service: Storage) -> None:
    """`brain` lane 由 brain.claim 独占，不许从通用入口拿 —— 那条路没有 fencing 校验。"""
    with pytest.raises(ValueError, match="Invalid lease lane"):
        _acquire(service, "brain", "worker-a")


def test_a_release_lets_the_next_worker_in(service: Storage) -> None:
    fence = _acquire(service, "consumer:user", "worker-a")
    service.call("lease.release", user_id="u1", fence=fence, lane="consumer:user", ttl=60)

    assert _acquire(service, "consumer:user", "worker-b") is not None


def test_a_renewal_pushes_the_expiry_out(service: Storage) -> None:
    """续租要做的是把到期时间往后推，否则长任务会被自己的租约掐断。"""
    fence = _acquire(service, "compaction", "worker-a", ttl=60)
    before = _expires(service, "compaction")

    service.call("lease.renew", user_id="u1", fence=fence, lane="compaction", ttl=600)

    assert _expires(service, "compaction") - before == pytest.approx(540, abs=5)


def _expires(service: Storage, lane: str) -> float:
    with service.transaction() as db:
        row = db.execute("SELECT expires FROM leases WHERE lane=?", (lane,)).fetchone()
        return float(row[0])


def test_a_lease_that_expired_mid_flight_is_already_lost(service: Storage) -> None:
    """到期不是「等别人来抢」才算数：持有者自己就该立刻写不下去。"""
    fence = _acquire(service, "compaction", "worker-a", ttl=60)
    with service.transaction() as db:
        db.execute("UPDATE leases SET expires=? WHERE user_id='u1'", (time.time() - 1,))

    with pytest.raises(LeaseLost, match="expired or replaced"):
        service.call("lease.renew", user_id="u1", fence=fence, lane="compaction", ttl=60)


def test_an_operation_that_needs_a_lease_refuses_to_run_without_one(service: Storage) -> None:
    with pytest.raises(LeaseLost, match="required"):
        service.call("lease.renew", user_id="u1", fence=None, lane="compaction", ttl=60)


def test_a_wrong_owner_on_a_real_fence_is_refused(service: Storage) -> None:
    """fence 的四个字段一起构成身份，不能只对 token。"""
    fence = _acquire(service, "compaction", "worker-a")
    forged = {**fence, "owner": "worker-b"}

    with pytest.raises(LeaseLost):
        service.call("lease.renew", user_id="u1", fence=forged, lane="compaction", ttl=60)


def test_a_check_with_no_fence_is_only_an_error_when_required(service: Storage) -> None:
    # `check` 还没有注解；直接调它是因为 required=False 那条路没有 RPC 入口。
    with service.transaction() as db:
        scheduling.check(db, "u1", None)  # type: ignore[no-untyped-call]
        with pytest.raises(LeaseLost):
            scheduling.check(db, "u1", None, required=True)  # type: ignore[no-untyped-call]


def test_a_lease_does_not_reach_across_users(service: Storage) -> None:
    """u1 的 lane 占着，不影响 u2 —— 用户之间不该互相排队。"""
    _acquire(service, "consumer:user", "worker-a", user_id="u1")

    assert _acquire(service, "consumer:user", "worker-a", user_id="u2") is not None
