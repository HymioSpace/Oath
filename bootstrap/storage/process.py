"""SQLite owner 的启动装配。"""

from bootstrap.common.authentication import service_token
from bootstrap.common.rpc import serve_rpc
from core.storage.database import Database
from core.storage.service import StorageService

OPERATIONS = {
    "subagent.spawn",
    "subagent.list",
    "subagent.get",
    "subagent.cancel",
    "subagent.continue",
    "subagent.notify",
    "state.read",
    "state.entries",
    "state.summary",
    "state.commit",
    "state.patch",
    "users.list",
    "tools.users",
    "queue.add",
    "message.react",
    "relay.claim",
    "relay.confirm",
    "consumer.claim",
    "buffer.add",
    "buffer.peek",
    "buffer.remove",
    "wake",
    "brain.claim",
    "brain.finish",
    "lease.acquire",
    "lease.renew",
    "lease.release",
    "supervisor.tick",
    "outbox.append",
    "outbox.read",
    "compaction.events",
    "history.page",
    "client.attach",
    "client.ack",
    "client.delivered",
    "detail.put",
    "detail.get",
    "trace.append",
    "trace.list",
    "trace.get",
    "journal.append",
    "journal.read",
    "snapshot.put",
    "snapshot.get",
}


def run(config):
    serve_rpc(
        StorageService(Database(config.database_path)),
        service_token(config),
        OPERATIONS,
        config.services.storage_url,
        bind_host=config.services.bind_host,
        service_name="storage",
    )
