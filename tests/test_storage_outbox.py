"""两个 outbox 与它们的投递边界。

`broker_outbox` 是 Storage 到 broker 的这一段，`outbox_events` 是到客户端的那一段。
两段各有一套确认机制，共有的不变量是同一条输入只落一次账、以及没被下游持久接纳
之前不从这里删掉。
"""

from __future__ import annotations

import pytest

from core.protocol.datatype.inbound import Inbound
from core.protocol.interface.storage import LeaseLost
from core.utils.encoding import JsonObject
from tests._support.storage import Storage


def _message(source_id: str = "m1", kind: str = "user") -> JsonObject:
    return Inbound(kind, "user.message", {"text": "hi"}, source_id).to_dict()


# --------------------------------------------------------------------------
# broker_outbox：先认领再确认
# --------------------------------------------------------------------------


def test_a_claim_hands_out_the_oldest_unsent_record(service: Storage) -> None:
    """按 id 排 —— 重放不能打乱因果。"""
    service.call("queue.add", user_id="u1", item=_message("m1"))
    service.call("queue.add", user_id="u1", item=_message("m2"))

    first = service.call("relay.claim", owner="relay-a", ttl=30)

    assert first["message"]["identity"] == Inbound("user", "user.message", {}, "m1").identity


def test_an_empty_outbox_claims_nothing_but_still_takes_the_lease(service: Storage) -> None:
    """空的时候也要占住租约 —— 否则两个 relay 会同时轮询。"""
    assert service.call("relay.claim", owner="relay-a", ttl=30)["message"] is None


def test_the_same_owner_claiming_again_keeps_its_token(service: Storage) -> None:
    """续领不是换主。token 变了的话，手里那条未确认的消息就成了旧 token。"""
    service.call("queue.add", user_id="u1", item=_message("m1"))

    first = service.call("relay.claim", owner="relay-a", ttl=30)
    second = service.call("relay.claim", owner="relay-a", ttl=30)

    assert second["token"] == first["token"]


def test_a_live_lease_stops_a_second_relay(service: Storage) -> None:
    service.call("relay.claim", owner="relay-a", ttl=30)

    assert service.call("relay.claim", owner="relay-b", ttl=30) is None


def test_confirming_marks_the_record_sent_so_it_is_not_offered_again(service: Storage) -> None:
    service.call("queue.add", user_id="u1", item=_message("m1"))
    claim = service.call("relay.claim", owner="relay-a", ttl=30)
    record = claim["message"]

    service.call(
        "relay.confirm", owner="relay-a", token=claim["token"], id=record["id"], user_id="u1"
    )

    assert service.call("relay.claim", owner="relay-a", ttl=30)["message"] is None


def test_an_unconfirmed_record_stays_pending_for_the_next_relay(service: Storage) -> None:
    """投出去但没确认 = 不确定 broker 收没收到。换 relay 时要重投，不能当没发生过。"""
    service.call("queue.add", user_id="u1", item=_message("m1"))
    first = service.call("relay.claim", owner="relay-a", ttl=-1)

    taken = service.call("relay.claim", owner="relay-b", ttl=30)

    assert taken["token"] > first["token"]
    assert taken["message"] is not None


def test_a_confirm_from_a_replaced_relay_is_refused(service: Storage) -> None:
    """旧 relay 的确认到达时，租约已经易主 —— 那条消息的归属不该由它决定。"""
    service.call("queue.add", user_id="u1", item=_message("m1"))
    stale = service.call("relay.claim", owner="relay-a", ttl=-1)
    service.call("relay.claim", owner="relay-b", ttl=30)

    with pytest.raises(LeaseLost, match="Publisher lease"):
        service.call(
            "relay.confirm",
            owner="relay-a",
            token=stale["token"],
            id=stale["message"]["id"],
            user_id="u1",
        )


def test_a_confirm_with_a_mismatched_token_is_refused(service: Storage) -> None:
    service.call("queue.add", user_id="u1", item=_message("m1"))
    service.call("relay.claim", owner="relay-a", ttl=30)

    with pytest.raises(LeaseLost):
        service.call("relay.confirm", owner="relay-a", token=99, id=1, user_id="u1")


def test_the_same_identity_is_only_queued_once(service: Storage) -> None:
    """重发同一批输入不该产生两条消息 —— 队列会投递两次，consumer 认两次。"""
    service.call("queue.add", user_id="u1", item=_message("m1"))
    service.call("queue.add", user_id="u1", item=_message("m1"))

    claim = service.call("relay.claim", owner="relay-a", ttl=30)
    service.call(
        "relay.confirm",
        owner="relay-a",
        token=claim["token"],
        id=claim["message"]["id"],
        user_id="u1",
    )

    assert service.call("relay.claim", owner="relay-a", ttl=30)["message"] is None


def test_two_users_queue_the_same_message_independently(service: Storage) -> None:
    """identity 的唯一性是 (user, identity) —— u1 和 u2 可以有同名消息。"""
    service.call("queue.add", user_id="u1", item=_message("m1"))
    service.call("queue.add", user_id="u2", item=_message("m1"))

    claim = service.call("relay.claim", owner="relay-a", ttl=30)
    service.call(
        "relay.confirm",
        owner="relay-a",
        token=claim["token"],
        id=claim["message"]["id"],
        user_id="u1",
    )

    assert service.call("relay.claim", owner="relay-a", ttl=30)["message"] is not None


# --------------------------------------------------------------------------
# inbox：buffer 的读与删分开
# --------------------------------------------------------------------------


def test_peeking_does_not_take_the_item_out(service: Storage) -> None:
    """读和删分开，brain 认领前那一批还住在 buffer 里 —— 崩了也还在。"""
    fence = service.call("lease.acquire", user_id="u1", lane="consumer:user", owner="w", ttl=60)
    service.call("buffer.add", user_id="u1", fence=fence, item=_message("m1"))

    assert len(service.call("buffer.peek", user_id="u1", kind="user")) == 1
    assert len(service.call("buffer.peek", user_id="u1", kind="user")) == 1


def test_buffering_the_same_input_twice_keeps_one_copy(service: Storage) -> None:
    fence = service.call("lease.acquire", user_id="u1", lane="consumer:user", owner="w", ttl=60)
    service.call("buffer.add", user_id="u1", fence=fence, item=_message("m1"))
    service.call("buffer.add", user_id="u1", fence=fence, item=_message("m1"))

    assert len(service.call("buffer.peek", user_id="u1", kind="user")) == 1


def test_a_buffer_write_needs_the_consumer_lease(service: Storage) -> None:
    """没 lease 就能往别人的 buffer 里塞输入，隔离就没了。"""
    with pytest.raises(LeaseLost, match="required"):
        service.call("buffer.add", user_id="u1", fence=None, item=_message("m1"))


def test_an_unclaimed_input_cannot_be_removed(service: Storage) -> None:
    """先落 journal 再从 buffer 删。顺序反了就会删掉还没成为事实的东西。"""
    fence = service.call("lease.acquire", user_id="u1", lane="consumer:user", owner="w", ttl=60)
    service.call("buffer.add", user_id="u1", fence=fence, item=_message("m1"))

    with pytest.raises(ValueError, match="unclaimed"):
        service.call(
            "buffer.remove",
            user_id="u1",
            fence=fence,
            identity=Inbound("user", "user.message", {}, "m1").identity,
        )


def test_removing_takes_the_one_named_and_leaves_the_rest(service: Storage) -> None:
    fence = service.call("lease.acquire", user_id="u1", lane="consumer:user", owner="w", ttl=60)
    service.call("buffer.add", user_id="u1", fence=fence, item=_message("m1"))
    service.call("buffer.add", user_id="u1", fence=fence, item=_message("m2"))
    claimed = Inbound("user", "user.message", {}, "m1").identity
    _claim(service, claimed)

    service.call("buffer.remove", user_id="u1", fence=fence, identity=claimed)

    left = service.call("buffer.peek", user_id="u1", kind="user")
    assert [i["source_id"] for i in left] == ["m2"]


def _claim(service: Storage, identity: str, user_id: str = "u1") -> None:
    """把一条输入标成已认领 —— 真实路径是 state.commit，这里只需要那个状态位。"""
    import json

    from core.storage.database import encode

    with service.transaction() as db:
        state = json.loads(
            db.execute("SELECT state_json FROM users WHERE user_id=?", (user_id,)).fetchone()[0]
        )
        state["inputs"][identity] = {"seq": 1}
        db.execute("UPDATE users SET state_json=? WHERE user_id=?", (encode(state), user_id))


# --------------------------------------------------------------------------
# outbox_events：客户端可见的事件
# --------------------------------------------------------------------------


def test_events_get_monotonic_seq_per_user(service: Storage) -> None:
    first = service.call("outbox.append", user_id="u1", type="assistant.message", payload={"a": 1})
    second = service.call("outbox.append", user_id="u1", type="assistant.message", payload={})

    assert second == first + 1


def test_compaction_events_are_readable_across_users_for_admin(service: Storage) -> None:
    service.call(
        "outbox.append",
        user_id="u1",
        type="context.compaction",
        payload={"compression_id": "c1", "status": "started", "mode": "async"},
        key="compaction:c1:started",
    )
    service.call(
        "outbox.append",
        user_id="u1",
        type="assistant.message",
        payload={"content": "not an admin compaction record"},
        key="assistant",
    )

    events = service.call(
        "compaction.events",
        since="2000-01-01T00:00:00.000000Z",
    )

    assert len(events) == 1
    assert events[0]["user_id"] == "u1"
    assert events[0]["event_type"] == "context.compaction"
    assert events[0]["payload"]["compression_id"] == "c1"


def test_the_same_key_does_not_produce_two_events(service: Storage) -> None:
    """重连重发是常态。同一个幂等键换内容则是 bug，要显式报错而不是覆盖。"""
    first = service.call("outbox.append", user_id="u1", type="t", payload={"a": 1}, key="k1")
    same = service.call("outbox.append", user_id="u1", type="t", payload={"a": 1}, key="k1")

    assert same == first

    with pytest.raises(ValueError, match="idempotency"):
        service.call("outbox.append", user_id="u1", type="t", payload={"a": 2}, key="k1")


def test_a_client_reads_only_what_it_has_not_seen(service: Storage) -> None:
    for i in range(3):
        service.call("outbox.append", user_id="u1", type="t", payload={"i": i})

    events = service.call("outbox.read", user_id="u1", after=1)

    assert [e["event_seq"] for e in events] == [2, 3]


def test_reading_someone_elses_events_returns_nothing(service: Storage) -> None:
    service.call("outbox.append", user_id="u1", type="t", payload={})

    assert service.call("outbox.read", user_id="u2", after=0) == []


def test_an_ack_cannot_run_past_what_was_delivered(service: Storage) -> None:
    """确认没收到的东西会让客户端永久跳过那批事件。"""
    service.call("outbox.append", user_id="u1", type="t", payload={})
    attached = service.call("client.attach", user_id="u1", client_id="c1")

    with pytest.raises(ValueError, match="exceeds delivered"):
        service.call(
            "client.ack",
            user_id="u1",
            client_id="c1",
            generation=attached["generation"],
            seq=1,
        )


def test_a_replaced_client_cannot_still_ack(service: Storage) -> None:
    """同一用户重连会换 generation。旧连接的回执不该推进新连接的游标。"""
    service.call("outbox.append", user_id="u1", type="t", payload={})
    first = service.call("client.attach", user_id="u1", client_id="c1")
    service.call(
        "client.delivered", user_id="u1", client_id="c1", generation=first["generation"], seq=1
    )
    service.call("client.attach", user_id="u1", client_id="c1")

    with pytest.raises(ValueError, match="client_replaced"):
        service.call(
            "client.ack", user_id="u1", client_id="c1", generation=first["generation"], seq=1
        )


def test_a_reconnect_resumes_from_the_ack_point(service: Storage) -> None:
    """断线重连后从确认处接着收，不重发也不丢。"""
    for i in range(3):
        service.call("outbox.append", user_id="u1", type="t", payload={"i": i})
    first = service.call("client.attach", user_id="u1", client_id="c1")
    service.call(
        "client.delivered", user_id="u1", client_id="c1", generation=first["generation"], seq=2
    )
    service.call("client.ack", user_id="u1", client_id="c1", generation=first["generation"], seq=2)
    again = service.call("client.attach", user_id="u1", client_id="c1")

    assert again["after_cursor"] == 2
    assert [e["event_seq"] for e in service.call("outbox.read", user_id="u1", after=2)] == [3]


def test_a_client_id_has_to_be_a_usable_string(service: Storage) -> None:
    for bad in ("", "x" * 129, None, 7):
        with pytest.raises(ValueError, match="client_id"):
            service.call("client.attach", user_id="u1", client_id=bad)


def test_trace_metadata_does_not_conflict_with_retry(service: Storage) -> None:
    first = _message("context-retry")
    first["telemetry"] = {"trace_id": "first", "received_at_unix_ms": 1}
    retry = {**first, "telemetry": {"trace_id": "second", "received_at_unix_ms": 2}}
    original = service.call("queue.add", user_id="u1", item=first)
    assert service.call("queue.add", user_id="u1", item=retry) == original
    changed = {**retry, "payload": {**retry["payload"], "content": "changed"}}
    with pytest.raises(ValueError, match="idempotency conflict"):
        service.call("queue.add", user_id="u1", item=changed)
