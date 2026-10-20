"""有界 Worker 池：每次只领取一个用户的一个 step。"""

import logging

from bootstrap.clients.service_client import ServiceClient
from bootstrap.common.assembly import build_context
from bootstrap.common.authentication import service_token
from bootstrap.common.signals import shutdown_event
from bootstrap.common.vault import VaultSecrets
from core.compaction import CompactionCoordinator, LlmCompactor
from core.journal.runtime_store import RuntimeStore
from core.llm.configured_provider import ConfiguredProvider
from core.scheduling.worker import run_workers
from core.storage.adapters import DurableEventSink

_log = logging.getLogger(__name__)


def run(config):
    stop = shutdown_event()
    token = service_token(config)
    storage = ServiceClient(config.services.storage_url, token, config.services.request_timeout)
    queue = ServiceClient(config.services.queue_url, token, config.services.request_timeout)
    manager = ServiceClient(config.services.manager_url, token, config.services.request_timeout)
    tools = ServiceClient(config.services.tools_url, token, config.services.request_timeout)

    secrets = VaultSecrets(
        config.secrets.vault_url,
        config.secrets.role_id_file,
        config.secrets.secret_id_file,
    )
    provider = ConfiguredProvider(config.agent, secrets)
    compaction = CompactionCoordinator(
        RuntimeStore(storage),
        LlmCompactor(
            provider,
            model=config.agent.provider.model,
            max_output_tokens=config.agent.max_output_tokens,
        ),
        config.agent,
        sink=DurableEventSink(storage),
        workers=config.services.workers,
        lease_seconds=config.services.lease_seconds,
    )

    try:
        run_workers(
            stop,
            manager,
            lambda fence: build_context(config, storage, queue, tools, fence),
            provider,
            compaction,
            ttl=config.services.lease_seconds,
            poll=config.services.poll_seconds,
            workers=config.services.workers,
        )
    finally:
        compaction.shutdown()
        secrets.close()
        for client in (storage, queue, manager, tools):
            client.close()
