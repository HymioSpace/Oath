"""Biz 独立读取共享用户状态，不依赖某个 Agent 进程在线。"""

import time

from biz.application import BizApplication
from bootstrap.common.authentication import UserAuthenticator
from bootstrap.common.extension_loader import load_extensions
from core.context.user_context import current, table_of
from core.storage.adapters import StoredTraces


class StoredAgentAccess:
    def __init__(self, storage):
        self.storage = storage

    def agent_operation(self, user, operation, **parameters):
        return self.storage.call(operation, user_id=user.storage_id, **parameters)

    def conversation_history(self, user, *, before, limit):
        return self.storage.call(
            "history.page", user_id=user.storage_id, before=before, limit=limit
        )

    def compaction_events(self, *, since, limit=5000):
        return self.storage.call("compaction.events", since=since, limit=limit)

    def user_context(self, user):
        state = self.storage.call("state.read", user_id=user.storage_id)["state"]
        return current(table_of(state), now=time.time())


def build_application(config, storage, *, cli=False):
    commands = load_extensions(
        config.agent.extension_dirs,
        kinds=frozenset({"cli"}),
        cli_dirs=config.agent.cli_dirs,
    ).commands
    return BizApplication(
        StoredAgentAccess(storage),
        UserAuthenticator(config, cli=cli),
        commands,
        StoredTraces(storage),
    )
