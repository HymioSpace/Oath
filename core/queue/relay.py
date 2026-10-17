"""Transactional outbox -> publisher confirm -> sent. Ambiguity means redelivery."""

import logging
import uuid

from core.queue.rabbitmq import connect, publish

_log = logging.getLogger(__name__)


def relay(storage, broker_url, stop):
    owner = uuid.uuid4().hex
    while not stop.is_set():
        connection = None
        try:
            connection, channel = connect(broker_url)
            channel.confirm_delivery()
            while not stop.is_set():
                claim = storage.call("relay.claim", owner=owner, ttl=45)
                if claim and claim["message"]:
                    publish(channel, claim["message"])
                    storage.call(
                        "relay.confirm",
                        owner=owner,
                        token=claim["token"],
                        id=claim["message"]["id"],
                    )
                else:
                    # time_limit 的单位是秒，运行时签名是 Optional[float]；types-pika 的
                    # stub 还写着 int。
                    connection.process_data_events(time_limit=0.25)  # type: ignore[arg-type]
        except Exception:
            _log.exception("Broker relay interrupted; unconfirmed records remain pending")
            stop.wait(1)
        finally:
            if connection and connection.is_open:
                connection.close()
