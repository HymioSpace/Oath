"""工具服务装配；并发与工具生命周期属于 core.tool。"""

from dataclasses import asdict

from bootstrap.clients.service_client import ServiceClient
from bootstrap.common.authentication import cli_token, service_token
from bootstrap.common.extension_loader import load_extensions
from bootstrap.common.rpc import serve_rpc
from core.detail import DETAIL_READ, DetailReadTool, ResultShrinker
from core.journal.runtime_store import RuntimeStore
from core.protocol.datatype.tool import ToolCallRequest
from core.protocol.datatype.user import User
from core.protocol.interface.permission import PermissionPolicy
from core.shell import BashTool, HostEnvironment
from core.skill import SKILL, SkillSystem, SkillTool
from core.storage.adapters import DurableEventSink, StoredDetails, StoredQueue
from core.tool.impl import ThreadedToolSystem
from core.tool.registry import ToolRegistry


class AllowAllPolicy(PermissionPolicy):
    def check(self, user, tool, arguments):
        return "allow"


class ToolService:
    def __init__(self, tools):
        self.tools = tools

    def call(self, operation, **p):
        if operation == "tools.definitions":
            return [d.to_dict() for d in self.tools.definitions()]
        user = User(p["user_id"])
        if operation == "tools.submit":
            if not p.get("fence") or p["fence"]["lane"] != "brain":
                raise ValueError("Submitting a batch requires the active Brain lease")
            with self.tools.store.fenced(p["fence"]):
                return asdict(
                    self.tools.submit_batch(
                        user, p["batch_id"], [ToolCallRequest(**r) for r in p["requests"]]
                    )
                )
        if operation == "tools.permission":
            if p["decision"] not in {"approved", "denied", "expired"}:
                raise ValueError("Invalid approval decision")
            return self.tools.apply_permission(
                user, p["invocation_id"], p["decision"], p["command_id"]
            )
        if operation == "tools.cancel":
            return self.tools.cancel(user, p.get("run_id"))
        if operation == "tools.batch":
            return self.tools.batch_status(user, p["batch_id"])
        raise ValueError("Unknown tool operation")


def run(config):
    token = service_token(config)
    storage = ServiceClient(config.services.storage_url, token, config.services.request_timeout)
    queue = ServiceClient(config.services.queue_url, token, config.services.request_timeout)
    extensions = load_extensions(config.agent.extension_dirs)
    registry = ToolRegistry(default_timeout_seconds=config.agent.tool_timeout_seconds)
    details = StoredDetails(storage)
    registry.register(DetailReadTool(details))
    skills = SkillSystem(
        (config.agent.skill_dir, *extensions.skill_dirs), scan_dirs=config.agent.skill_scan_dirs
    )
    registry.register(SkillTool(skills))
    env = HostEnvironment(config.agent.workspace_root, bin_dirs=(config.agent.cli_bin_dir,))

    def cli_env(user):
        return {
            "OATH_CLI_SERVER": config.services.biz_url,
            "OATH_CLI_TOKEN": cli_token(config, user),
        }

    registry.register(BashTool(env, cli_env, root=config.agent.workspace_root))
    for tool in extensions.tools:
        registry.register(tool)
    tools = ThreadedToolSystem(
        registry,
        DurableEventSink(storage),
        AllowAllPolicy(),
        store=RuntimeStore(storage),
        queue=StoredQueue(queue),
        shrinker=ResultShrinker(
            details, config.agent.tool_result_detail_chars, whole=frozenset({DETAIL_READ, SKILL})
        ),
    )
    try:
        serve_rpc(
            ToolService(tools),
            token,
            {
                "tools.definitions",
                "tools.submit",
                "tools.permission",
                "tools.cancel",
                "tools.batch",
            },
            config.services.tools_url,
            bind_host=config.services.bind_host,
        )
    finally:
        tools.shutdown()
        queue.close()
        storage.close()
