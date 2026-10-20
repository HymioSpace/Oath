"""Broker delivery and durable buffer timers; no per-user resident loops."""

import json
import logging
import time
import uuid

from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.user import User
from core.queue.rabbitmq import connect
from core.scheduling.leases import DurableWake, renewed

_log = logging.getLogger(__name__)


def consume(consumer_type, storage, manager, context_factory, broker_url, stop, ttl, poll):
    owner = uuid.uuid4().hex
    kind = consumer_type.queue_name

    def evaluate(fence, item=None):
        user = User.from_storage_id(fence["user_id"])
        try:
            with renewed(storage, fence, ttl):
                consumer = consumer_type(context_factory(fence), DurableWake(manager))
                if item:
                    consumer.receive(user, item)
                consumer.evaluate(user, item is not None)
        finally:
            storage.call("lease.release", user_id=user.storage_id, fence=fence)

    while not stop.is_set():
        connection = None
        try:
            connection, channel = connect(broker_url)
            channel.basic_qos(prefetch_count=1)

            def delivered(ch, method, properties, body):
                try:
                    envelope = json.loads(body)
                    user = User.from_storage_id(envelope["user_id"])
                    item = Inbound.from_dict(envelope["item"])
                    if item.kind != kind or not item.source_id:
                        raise ValueError("Invalid queue envelope")
                except (ValueError, TypeError, KeyError):
                    _log.error("Malformed message rejected to dead-letter queue")
                    ch.basic_reject(method.delivery_tag, requeue=False)
                    return
                fence = storage.call(
                    "lease.acquire",
                    user_id=user.storage_id,
                    lane="consumer:" + kind,
                    owner=owner,
                    ttl=ttl,
                )
                if not fence:
                    stop.wait(poll)
                    ch.basic_nack(method.delivery_tag, requeue=True)
                    return
                evaluate(fence, item)
                ch.basic_ack(method.delivery_tag)

            channel.basic_consume(f"oath.{kind}", delivered, auto_ack=False)
            next_scan = 0
            while not stop.is_set():
                connection.process_data_events(time_limit=poll)
                if time.monotonic() >= next_scan:
                    fence = storage.call("consumer.claim", kind=kind, owner=owner, ttl=ttl)
                    if fence:
                        evaluate(fence)
                    next_scan = time.monotonic() + poll
        except Exception:
            _log.exception("Consumer interrupted; broker retains unacknowledged deliveries")
            stop.wait(1)
        finally:
            if connection and connection.is_open:
                connection.close()
