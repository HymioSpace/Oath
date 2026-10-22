"""Consumer service dependency assembly."""

import threading

from bootstrap.clients.service_client import ServiceClient
from bootstrap.common.assembly import build_context
from bootstrap.common.authentication import service_token
from bootstrap.common.signals import shutdown_event
from core.consumer import SystemConsumer, ToolConsumer, UserConsumer
from core.intake.service import consume
from core.observability import start_metrics_server


def run(config):
    start_metrics_server()
    stop = shutdown_event()
    token = service_token(config)
    storage, queue, manager, tools = [
        ServiceClient(url, token, config.services.request_timeout)
        for url in (
            config.services.storage_url,
            config.services.queue_url,
            config.services.manager_url,
            config.services.tools_url,
        )
    ]

    def context(fence):
        return build_context(config, storage, queue, tools, fence)

    threads = [
        threading.Thread(
            target=consume,
            args=(
                kind,
                storage,
                manager,
                context,
                config.services.broker_url,
                stop,
                config.services.lease_seconds,
                config.services.poll_seconds,
            ),
            name=f"oath-{kind.queue_name}",
        )
        for kind in (UserConsumer, ToolConsumer, SystemConsumer)
    ]
    for thread in threads:
        thread.start()
    stop.wait()
    for thread in threads:
        thread.join()
    for client in (storage, queue, manager, tools):
        client.close()
