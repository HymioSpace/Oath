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

    def user_context(self, user):
        state = self.storage.call("state.read", user_id=user.user_id)["state"]
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
