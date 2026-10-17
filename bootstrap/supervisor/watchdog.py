"""全局 Supervisor 服务装配。"""

import logging

from bootstrap.clients.service_client import ServiceClient
from bootstrap.common.authentication import service_token
from bootstrap.common.signals import shutdown_event
from core.supervisor.supervisor import Supervisor


def run(config):
    stop = shutdown_event()
    storage = ServiceClient(
        config.services.storage_url, service_token(config), config.services.request_timeout
    )
    supervisor = Supervisor(storage, config.supervisor.invoke_interval_seconds)
    try:
        while not stop.is_set():
            try:
                supervisor.tick()
            except Exception:
                logging.exception("Supervisor tick failed")
            stop.wait(1)
    finally:
        storage.close()
