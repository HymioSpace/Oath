"""Transactional outbox -> publisher confirm -> sent. Ambiguity means redelivery."""

import json
import logging
import time
import uuid
from datetime import datetime

from core.observability import QUEUE_PUBLISH_DELAY
from core.observability.spans import append_span
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
                    try:
                        item = json.loads(claim["message"]["item_json"])
                        accepted = _unix_seconds(item.get("occur_dt"))
                        if accepted is not None:
                            QUEUE_PUBLISH_DELAY.labels(claim["message"]["kind"]).observe(
                                max(0, time.time() - accepted)
                            )
                            telemetry = item.get("telemetry")
                            trace_id = (
                                telemetry.get("trace_id") if isinstance(telemetry, dict) else ""
                            )
                            if isinstance(trace_id, str):
                                append_span(
                                    storage,
                                    user_id=claim["message"]["user_id"],
                                    trace_id=trace_id,
                                    name="queue.publish",
                                    started_ns=int(accepted * 1_000_000_000),
                                    ended_ns=time.time_ns(),
                                    attributes={"oath.queue.kind": claim["message"]["kind"]},
                                )
                    except (TypeError, ValueError, KeyError):
                        pass
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


def _unix_seconds(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
