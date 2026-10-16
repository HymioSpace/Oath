"""`RedisEventQueue` 独有的那部分：延迟确认是**逐条**的。

`EventQueue` 的契约测的是接口（`test_event_queue.py`），这里测中间件自己的语义 —— 消费组、
pending、`XAUTOCLAIM`、以及确认的粒度。

**为什么逐条确认。** consumer 把一批取回来之后是逐条写进 buffer 的，中间一条失败就抛走。
确认如果按批来，那批里还没落盘的几条会被一起 ack 掉，从此谁都不再管 —— 它们既不在 buffer
里，也不在队列里。

```
OATH_TEST_REDIS=redis://127.0.0.1:6379/15 pytest tests/test_redis_queue_confirm.py
```

要真的 Redis：队列是 Redis Streams，这一段绕不过去。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from redis import Redis

from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import QueueName
from core.protocol.datatype.scope import Scope
from core.queue import RedisEventQueue

REDIS = os.environ.get("OATH_TEST_REDIS", "redis://127.0.0.1:6379/15")


def _reachable() -> bool:
    client = Redis.from_url(REDIS, socket_connect_timeout=0.5)
    try:
        return bool(client.ping())
    except Exception:
        return False
    finally:
        client.close()


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not _reachable(), reason="需要真 Redis：队列是 Redis Streams"),
]


@pytest.fixture
def client() -> Iterator[Redis]:
    instance = Redis.from_url(REDIS)
    yield instance
    instance.close()


@pytest.fixture
def scope() -> Scope:
    # 每个用例一个 scope，key 天然隔离，跑完不用清 Redis。
    return Scope("u1", uuid4().hex[:16])


def _message(index: int, queue: str = QueueName.USER) -> Inbound:
    return Inbound(queue, "user.message", {"content": f"内容{index}"}, f"m{index}")


def _ids(items: tuple[Inbound, ...]) -> list[str]:
    return [item.source_id for item in items]


def _listen_all(queue: RedisEventQueue, scope: Scope, count: int) -> None:
    """新消息是 `listen` 拉进 pending 的，`get_all` 只从 pending 里取。少这一步就是空的。"""
    for _ in range(count):
        queue.listen(scope, QueueName.USER, 0.05)


def _pending(client: Redis, scope: Scope, queue: str = QueueName.USER) -> int:
    """这个 key 上还挂在这个消费组 pending 里的条数。"""
    entries = client.xpending(f"oath:queue:{scope.user_id}:{scope.session_id}:{queue}", "oath")
    return int(entries["pending"])


def test_confirming_one_message_leaves_the_others_pending(client: Redis, scope: Scope) -> None:
    queue = RedisEventQueue(client)
    for index in range(3):
        queue.add(scope, _message(index))
    _listen_all(queue, scope, 3)

    batch = queue.get_all(scope, QueueName.USER)
    assert _ids(batch) == ["m0", "m1", "m2"]

    queue.confirm(scope, QueueName.USER, batch[0])

    assert _pending(client, scope) == 2, "确认一条不该把同批的另外两条也放掉"


def test_unconfirmed_messages_come_back_on_the_next_take(client: Redis, scope: Scope) -> None:
    """没确认的就是没收下。下一轮还要给一次机会 —— 它可能只是上一轮写 buffer 写失败了。"""
    queue = RedisEventQueue(client)
    for index in range(3):
        queue.add(scope, _message(index))
    _listen_all(queue, scope, 3)

    batch = queue.get_all(scope, QueueName.USER)
    queue.confirm(scope, QueueName.USER, batch[0])

    assert _ids(queue.get_all(scope, QueueName.USER)) == ["m1", "m2"]


def test_confirmed_messages_do_not_come_back(client: Redis, scope: Scope) -> None:
    queue = RedisEventQueue(client)
    for index in range(3):
        queue.add(scope, _message(index))
    _listen_all(queue, scope, 3)

    for item in queue.get_all(scope, QueueName.USER):
        queue.confirm(scope, QueueName.USER, item)

    assert queue.get_all(scope, QueueName.USER) == ()
    assert _pending(client, scope) == 0


def test_another_process_takes_over_what_was_never_confirmed(client: Redis, scope: Scope) -> None:
    """进程换掉了，consumer 名字也换了。上一轮没确认的 pending 还在，得被 `XAUTOCLAIM` 收回来。

    崩在写 buffer 之前的消息靠的就是这条：它从来没被确认过，所以从来没人把它 ack 掉。
    """
    first = RedisEventQueue(client)
    first.add(scope, _message(1))
    first.add(scope, _message(2))
    _listen_all(first, scope, 2)
    batch = first.get_all(scope, QueueName.USER)
    first.confirm(scope, QueueName.USER, batch[0])
    first.close()

    second = RedisEventQueue(client)

    assert _ids(second.get_all(scope, QueueName.USER)) == ["m2"]


def test_the_stream_does_not_grow_without_bound(client: Redis, scope: Scope) -> None:
    """确认过的消息要从 stream 里删掉 —— 不删的话 stream 只增不减，长会话会一直涨。"""
    queue = RedisEventQueue(client)
    for index in range(20):
        queue.add(scope, _message(index))
    _listen_all(queue, scope, 20)

    for _ in range(4):
        for item in queue.get_all(scope, QueueName.USER):
            queue.confirm(scope, QueueName.USER, item)

    key = f"oath:queue:{scope.user_id}:{scope.session_id}:{QueueName.USER}"
    assert client.xlen(key) == 0
