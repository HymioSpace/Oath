"""Queue ingress + relay; RabbitMQ owns message delivery and acknowledgements."""

import threading

from bootstrap.clients.service_client import ServiceClient
from bootstrap.common.authentication import service_token
from bootstrap.common.rpc import serve_rpc
from core.queue.relay import relay


def run(config):
    token = service_token(config)
    storage = ServiceClient(config.services.storage_url, token, config.services.request_timeout)
    stop = threading.Event()
    thread = threading.Thread(target=relay, args=(storage, config.services.broker_url, stop))
    thread.start()
    try:
        serve_rpc(
            storage,
            token,
            {"queue.add"},
            config.services.queue_url,
            bind_host=config.services.bind_host,
        )
    finally:
        stop.set()
        thread.join()
        storage.close()
