"""Build a biz application from a session manager, CLI declarations and the trace store."""

from biz.application import BizApplication
from bootstrap.clients.agent_client import AgentClient
from bootstrap.common.extension_loader import load_extensions
from bootstrap.manager import SessionManager
from core.protocol.datatype.config import OathConfig
from core.trace.impl import FileTraceStore


def build_application(config: OathConfig, manager: SessionManager) -> BizApplication:
    """把 manager 接成 biz 唯一的一根线。

    biz 不直接找 agent：地址与 session 级凭证都问 manager 要 —— 那两样东西只有它有，
    也不该在别处再存一份。
    """
    commands = load_extensions(config.agent.extension_dirs, kinds=frozenset({"cli"})).commands
    # trace 是与 journal 分离的一组只追加文件，读路径本就容忍写了一半的尾行：
    # biz 自己读，不必向 agent 绕一圈。store 自己按 scope 分目录，这里给根。
    traces = FileTraceStore(config.data_root / "traces")
    access = AgentClient(url_of=manager.session_url, credential_of=manager.session_credential)
    return BizApplication(access, manager, commands, traces)
