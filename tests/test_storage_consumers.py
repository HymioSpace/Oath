"""consumer 的认领与 supervisor 的周期唤醒。

consumer 从 inbox 里挑用户，两个要求：**公平**（没人能一直占着）和**排他**
（同一用户同一队列同时只有一个 consumer 在处理）。supervisor 只在真有输入的
用户身上推进 `next_invoke`，否则它会不停地往空 inbox 里灌 `system.invoke`。
"""

from __future__ import annotations

import json

from core.protocol.datatype.inbound import Inbound
from tests._support.storage import Storage


def _buffer(service: Storage, user_id: str, source_id: str, kind: str = "user") -> None:
    """往 inbox 放一条输入，然后把 consumer lease 还回去。

    写 buffer 需要 lease；留着它不放的话，后面的 claim 自然拿不到 —— 那是另一条用例。
    """
    fence = service.call(
        "lease.acquire", user_id=user_id, lane=f"consumer:{kind}", owner="w", ttl=60
    )
    service.call(
        "buffer.add",
        user_id=user_id,
        fence=fence,
        item=Inbound(kind, f"{kind}.message", {}, source_id).to_dict(),
    )
    service.call("lease.release", user_id=user_id, fence=fence, lane=f"consumer:{kind}", ttl=0)


# --------------------------------------------------------------------------
# consumer.claim
# --------------------------------------------------------------------------


def test_a_claim_picks_the_user_with_input_on_that_lane(service: Storage) -> None:
    _buffer(service, "u1", "m1", kind="user")

    assert service.call("consumer.claim", kind="user", owner="w", ttl=60)["user_id"] == "u1"


def test_a_lane_does_not_borrow_another_lanes_input(service: Storage) -> None:
    """tool 队列有输入不意味着 user 队列有 —— 两者唤醒条件不同。"""
    _buffer(service, "u1", "m1", kind="tool")

    assert service.call("consumer.claim", kind="user", owner="w", ttl=60) is None
    assert service.call("consumer.claim", kind="tool", owner="w", ttl=60) is not None


def test_a_held_consumer_lease_moves_the_work_to_the_next_user(service: Storage) -> None:
    """u1 正在被别的 consumer 处理时，u2 不该跟着一起空转。"""
    _buffer(service, "u1", "m1")
    _buffer(service, "u2", "m2")
    first = service.call("consumer.claim", kind="user", owner="w1", ttl=60)
    service.call("lease.renew", user_id=first["user_id"], fence=first, lane="consumer:user", ttl=60)

    assert service.call("consumer.claim", kind="user", owner="w2", ttl=60)["user_id"] == "u2"


def test_the_user_whose_input_was_taken_longest_ago_comes_first(service: Storage) -> None:
    """按 touched 排 —— 一个持续有新输入的用户不能把队列前面的人饿死。"""
    _buffer(service, "u1", "m1")
    _buffer(service, "u2", "m2")
    with service.transaction() as db:
        db.execute("UPDATE leases SET touched=100 WHERE user_id='u1'")
        db.execute("UPDATE leases SET touched=1 WHERE user_id='u2'")

    assert service.call("consumer.claim", kind="user", owner="w", ttl=60)["user_id"] == "u2"


def test_the_order_keeps_going_round_robin(service: Storage) -> None:
    """被认领就等于刚碰过，于是轮到下一个 —— 否则一个人会一直排第一。"""
    _buffer(service, "u1", "m1")
    _buffer(service, "u2", "m2")
    with service.transaction() as db:
        db.execute("UPDATE leases SET touched=1 WHERE user_id='u1'")
        db.execute("UPDATE leases SET touched=100 WHERE user_id='u2'")

    claimed = []
    for _ in range(2):
        fence = service.call("consumer.claim", kind="user", owner="w", ttl=60)
        claimed.append(fence["user_id"])
        service.call(
            "lease.renew", user_id=fence["user_id"], fence=fence, lane="consumer:user", ttl=60
        )

    assert claimed == ["u1", "u2"]


def test_a_claim_with_an_empty_inbox_returns_nothing(service: Storage) -> None:
    assert service.call("consumer.claim", kind="system", owner="w", ttl=60) is None


# --------------------------------------------------------------------------
# supervisor.tick
# --------------------------------------------------------------------------


def _queued(service: Storage, user_id: str = "u1") -> list[str]:
    """outbox 里排队的输入。tick 投的是队列，不是 inbox —— 要 consumer 取出来才算 buffer 里的事。"""
    claim = service.call("relay.claim", owner="relay", ttl=30)
    record = claim["message"]
    if record is None or record["user_id"] != user_id:
        return []
    service.call(
        "relay.confirm", owner="relay", token=claim["token"], id=record["id"], user_id=user_id
    )
    return [json.loads(record["item_json"])["source"]]


def test_a_due_user_gets_an_invoke_and_its_clock_moves_forward(service: Storage) -> None:
    _buffer(service, "u1", "m1", kind="system")

    assert service.call("supervisor.tick", interval=30) == 1
    assert _queued(service) == ["system.invoke"]


def test_a_user_without_input_is_left_alone(service: Storage) -> None:
    """空 inbox 收到 system.invoke 只会让 brain 白跑一轮。"""
    service.call("state.read", user_id="u1")

    assert service.call("supervisor.tick", interval=30) == 0


def test_a_tick_that_is_not_due_yet_does_nothing(service: Storage) -> None:
    _buffer(service, "u1", "m1", kind="system")
    with service.transaction() as db:
        db.execute("UPDATE users SET next_invoke=99999999999 WHERE user_id='u1'")

    assert service.call("supervisor.tick", interval=30) == 0


def test_twice_ticking_before_the_interval_does_not_double_invoke(service: Storage) -> None:
    """`next_invoke` 前进是幂等的来源 —— 否则一次 tick 就把周期缩掉一半。"""
    _buffer(service, "u1", "m1", kind="system")
    service.call("supervisor.tick", interval=30)
    assert _queued(service) == ["system.invoke"]

    assert service.call("supervisor.tick", interval=30) == 0
    assert _queued(service) == []


def test_the_invoke_is_repeatable_after_the_interval_passes(service: Storage) -> None:
    _buffer(service, "u1", "m1", kind="system")
    service.call("supervisor.tick", interval=30)
    with service.transaction() as db:
        db.execute("UPDATE users SET next_invoke=0 WHERE user_id='u1'")

    assert service.call("supervisor.tick", interval=30) == 1


def test_a_tick_reaches_each_due_user(service: Storage) -> None:
    """一批轮询，不是只服务第一个 —— 前面那个卡住不该挡住别人。"""
    _buffer(service, "u1", "m1", kind="system")
    _buffer(service, "u2", "m2", kind="system")

    assert service.call("supervisor.tick", interval=30) == 2
