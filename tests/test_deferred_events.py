import json
import threading
from types import SimpleNamespace

from starlette.testclient import TestClient

from bootstrap.biz.server import build_app
from core.consumer import UserConsumer
from core.intake.coordinator import IntakeCoordinator, new_intent
from core.journal.runtime_store import RuntimeStore
from core.protocol.datatype.config import AgentConfig
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.user import User
from core.storage.adapters import StoredBuffer


def _deferred(source_id="kit-1"):
    return Inbound(
        "user",
        "app.kit.interaction",
        {"kit_id": "demo", "operation": "items.create"},
        source_id,
        telemetry={"defer_wake": True},
    )


def test_deferred_user_input_is_buffered_without_requesting_a_wake():
    calls = []
    consumer = UserConsumer(
        SimpleNamespace(
            buffer=SimpleNamespace(peek=lambda user, queue: [_deferred()]),
        ),
        SimpleNamespace(wake=lambda user: calls.append(user)),
    )

    assert consumer.evaluate("user", delivered=False) == 5.0
    assert calls == []
    assert not new_intent("user", _deferred())
    assert new_intent(
        "user", Inbound("user", "user.message", {"content": "hello"}, "message-1")
    )


def test_deferred_user_input_does_not_wake_storage_but_normal_input_does(service):
    fence = service.call(
        "lease.acquire", user_id="u1", lane="consumer:user", owner="w", ttl=60
    )
    service.call(
        "buffer.add", user_id="u1", fence=fence, item=_deferred().to_dict()
    )
    with service.transaction() as db:
        counters = dict(
            db.execute(
                "SELECT requested,claimed,completed FROM scheduling WHERE user_id='u1'"
            ).fetchone()
        )
    assert counters == {"requested": 0, "claimed": 0, "completed": 0}

    service.call(
        "buffer.add",
        user_id="u1",
        fence=fence,
        item=Inbound(
            "user", "user.message", {"content": "hello"}, "message-1"
        ).to_dict(),
    )
    with service.transaction() as db:
        requested = db.execute(
            "SELECT requested FROM scheduling WHERE user_id='u1'"
        ).fetchone()[0]
    assert requested == 1


def test_deferred_user_input_joins_the_next_live_turn(service):
    deferred = _deferred()
    normal = Inbound("user", "user.message", {"content": "看看今天的记录"}, "message-1")
    fence = service.call(
        "lease.acquire", user_id="u1", lane="consumer:user", owner="w", ttl=60
    )
    buffer = StoredBuffer(service, fence)
    user = User("u1")
    buffer.add(user, "user", deferred)
    buffer.add(user, "user", normal)

    store = RuntimeStore(service)
    store.fence = service.call("brain.claim", owner="intake", ttl=60)
    ctx = SimpleNamespace(
        store=store,
        config=AgentConfig(),
        tools=SimpleNamespace(definitions=lambda **_: ()),
        buffer=buffer,
    )
    executor = SimpleNamespace(
        system_prompt=lambda channel="unknown": "Rules",
        open_step=lambda *args: None,
        emit=lambda *args: None,
    )
    step = IntakeCoordinator(ctx, executor, threading.Event()).claim(user)

    assert step is not None
    assert {deferred.identity, normal.identity} <= set(step["input_ids"])


def test_application_event_transport_can_defer_user_queue_delivery():
    calls = []

    class Service:
        authenticator = SimpleNamespace(
            authenticate=lambda token, supplied: SimpleNamespace(
                user_id=supplied, agent_id=""
            )
        )

        @staticmethod
        def agent_operation(user, operation, **payload):
            calls.append((user.user_id, operation, payload))
            return 42

    client = TestClient(
        build_app(Service(), SimpleNamespace(list_commands=lambda: ()))
    )
    response = client.post(
        "/events",
        headers={"Authorization": "Bearer opaque", "X-Oath-User-Id": "alice"},
        json={
            "type": "app.kit.interaction",
            "queue": "user",
            "defer_wake": True,
            "event_id": "kit-1",
            "payload": {"kit_id": "demo", "operation": "items.create"},
        },
    )
    assert response.status_code == 200
    user_id, operation, payload = calls[0]
    item = json.loads(json.dumps(payload["item"]))
    assert user_id == "alice"
    assert operation == "queue.add"
    assert item["kind"] == "user"
    assert item["source"] == "app.kit.interaction"
    assert item["telemetry"] == {"defer_wake": True}


def test_application_event_transport_rejects_deferred_system_delivery():
    class Service:
        authenticator = SimpleNamespace(
            authenticate=lambda token, supplied: SimpleNamespace(
                user_id=supplied, agent_id=""
            )
        )

        @staticmethod
        def agent_operation(user, operation, **payload):
            raise AssertionError("invalid event must not reach storage")

    client = TestClient(
        build_app(Service(), SimpleNamespace(list_commands=lambda: ()))
    )
    response = client.post(
        "/events",
        headers={"Authorization": "Bearer opaque", "X-Oath-User-Id": "alice"},
        json={
            "type": "app.kit.interaction",
            "queue": "system",
            "defer_wake": True,
            "event_id": "kit-1",
            "payload": {},
        },
    )
    assert response.status_code == 400
