"""Three durable logical queues. Connections are owned by their calling thread."""

import json

import pika

KINDS = ("user", "tool", "system")


def connect(url):
    params = pika.URLParameters(url)
    params.heartbeat = 60
    params.socket_timeout = 10
    params.stack_timeout = 15
    params.blocked_connection_timeout = 15
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    channel.exchange_declare("oath.dead", exchange_type="direct", durable=True)
    for kind in KINDS:
        channel.queue_declare(
            f"oath.dead.{kind}", durable=True, arguments={"x-queue-type": "quorum"}
        )
        channel.queue_bind(f"oath.dead.{kind}", "oath.dead", kind)
        channel.queue_declare(
            f"oath.{kind}",
            durable=True,
            arguments={
                "x-queue-type": "quorum",
                "x-single-active-consumer": True,
                "x-dead-letter-exchange": "oath.dead",
                "x-dead-letter-routing-key": kind,
                "x-delivery-limit": -1,
            },
        )
    return connection, channel


def publish(channel, message):
    item = json.loads(message["item_json"])
    channel.basic_publish(
        "",
        f"oath.{item['kind']}",
        json.dumps({"user_id": message["user_id"], "item": item}).encode(),
        pika.BasicProperties(
            content_type="application/json", delivery_mode=2, message_id=str(message["id"])
        ),
        mandatory=True,
    )
