"""工具服务装配；并发与工具生命周期属于 core.tool。"""

import hashlib
from dataclasses import asdict
from pathlib import Path

from bootstrap.clients.permission_notifier import PermissionNotifier
from bootstrap.clients.permission_policy import RemotePermissionPolicy
from bootstrap.clients.service_client import ServiceClient
from bootstrap.common.authentication import cli_claims, cli_token, service_token
from bootstrap.common.extension_loader import load_extensions
from bootstrap.common.rpc import serve_rpc
from core.detail import DETAIL_READ, DetailReadTool, ResultShrinker
from core.journal.runtime_store import RuntimeStore
from core.protocol.datatype.tool import ToolCallRequest
from core.protocol.datatype.user import User
from core.protocol.interface.permission import PermissionPolicy
from core.shell import BashTool, HostEnvironment
from core.shell.cli_tool import CliTool
from core.skill import SKILL, SkillSystem, SkillTool
from core.storage.adapters import DurableEventSink, StoredDetails, StoredQueue, StoredTraces
from core.subagents.tools import tools as subagent_tools
from core.tool.impl import ThreadedToolSystem
from core.tool.registry import ToolRegistry


class AllowAllPolicy(PermissionPolicy):
    def __init__(self, storage=None):
        self.storage = storage

    def check(self, request):
        if request.user.agent_id and self.storage:
            child = self.storage.call(
                "subagent.get",
                user_id=request.user.user_id,
                agent_id=request.user.agent_id,
            )
            if child["status"] != "running":
                return "deny"
        if not request.user.agent_id and request.tool == "subagent_notify":
            return "deny"
        return "allow"


class ToolService:
    def __init__(self, tools, config=None):
        self.tools, self.config = tools, config

    def call(self, operation, **p):
        if operation.startswith("tools.cli_"):
            claims = cli_claims(self.config, p["credential"])
            user = User(claims["user_id"], claims.get("agent_id", ""))
            allowed = {
                definition.name
                for definition in self.tools._registry.definitions(
                    delegated=bool(user.agent_id), channel=claims["channel"]
                )
            }
            if operation == "tools.cli_catalog":
                return {
                    "commands": [
                        d.to_dict()
                        for d in self.tools._registry.definitions(
                            delegated=bool(user.agent_id), channel=claims["channel"]
                        )
                        if d.name not in {"bash", "cli"}
                    ]
                }
            if operation == "tools.cli_detach":

                def detach(state):
                    call = state["calls"].get(p["invocation_id"])
                    if not call or call.get("parent_invocation_id") != claims["parent"]:
                        raise ValueError("Unknown CLI child")
                    call["inline"] = False
                    return call.get("terminal")

                terminal = self.tools.store.transact(user, "tool.cli_detach", detach)
                if terminal:
                    from core.protocol.datatype.inbound import Inbound

                    self.tools._queue.add(
                        user, Inbound("tool", "tool.terminal", terminal, terminal["event_id"])
                    )
                return {"status": "detached"}
            if operation == "tools.cli_status":
                result = self.tools.status(user, p["invocation_id"])
                return asdict(result) if result else {"status": "not_found"}
            if operation == "tools.cli_submit":
                name, arguments, key = p["name"], p["arguments"], p["request_id"]
                if (
                    name not in allowed
                    or name in {"bash", "cli"}
                    or not isinstance(arguments, dict)
                    or not isinstance(key, str)
                    or not 1 <= len(key) <= 128
                ):
                    raise ValueError("Invalid CLI invocation")
                parent = self.tools.store.entries(user, "calls", [claims["parent"]]).get(
                    claims["parent"]
                )
                if not parent or parent["tool"] not in {"bash", "cli"}:
                    raise ValueError("Unknown CLI parent")
                child = (
                    "cli-"
                    + hashlib.sha256((claims["parent"] + ":" + key).encode()).hexdigest()[:40]
                )
                existing = self.tools.store.entries(user, "calls", [child]).get(child)
                if existing and (existing["tool"] != name or existing["arguments"] != arguments):
                    raise ValueError("CLI request_id conflicts with an existing invocation")
                result = self.tools.submit_batch(
                    user,
                    child,
                    [
                        ToolCallRequest(
                            name,
                            arguments,
                            child,
                            child,
                            child,
                            parent["run_id"],
                            parent.get("channel", "unknown"),
                        )
                    ],
                    parent_invocation_id=claims["parent"],
                    inline=not p.get("async", False),
                )
                return {"invocation_id": child, "status": result.handles[0].state}
            raise ValueError("Unknown CLI operation")
        if operation == "tools.definitions":
            target = User.from_storage_id(p["user_id"]) if p.get("user_id") else None
            definitions = (
                self.tools.definitions(delegated=True, channel=p.get("channel", "unknown"))
                if target and target.agent_id
                else self.tools.definitions(channel=p.get("channel", "unknown"))
            )
            return [d.to_dict() for d in definitions]
        user = User.from_storage_id(p["user_id"])
        if operation == "tools.submit":
            if not p.get("fence") or p["fence"]["lane"] != "brain":
                raise ValueError("Submitting a batch requires the active Brain lease")
            if self.tools.exposed_tools and any(
                r["tool"] not in self.tools.exposed_tools for r in p["requests"]
            ):
                raise ValueError("Capability is available through CLI only")
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
                user,
                p["invocation_id"],
                p["decision"],
                p["command_id"],
                scope=p.get("scope", "once"),
                client_id=p.get("client_id", ""),
                channel=p.get("channel", "unknown"),
                reason=p.get("reason", ""),
                action_digest=p.get("action_digest", ""),
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
    extensions = load_extensions(
        config.agent.extension_dirs,
        kinds=frozenset({"tools", "skills"}),
        tool_dirs=config.agent.tool_dirs,
        skill_dirs=config.agent.skill_dirs,
    )
    registry = ToolRegistry(default_timeout_seconds=config.agent.tool_timeout_seconds)
    details = StoredDetails(storage)
    registry.register(DetailReadTool(details))
    for tool in subagent_tools(storage):
        registry.register(tool)
    skills = SkillSystem(
        (config.agent.skill_dir, *extensions.skill_dirs), scan_dirs=config.agent.skill_scan_dirs
    )
    registry.register(SkillTool(skills))
    registry.register(
        CliTool(Path(__file__).resolve().parents[2] / "scripts/oath-cli", config.services.biz_url)
    )
    env = HostEnvironment(config.agent.workspace_root, bin_dirs=(config.agent.cli_bin_dir,))

    def cli_env(user):
        return {
            "OATH_CLI_SERVER": config.services.biz_url,
            "OATH_CLI_TOKEN": cli_token(config, user),
        }

    if config.agent.builtin_bash:
        registry.register(BashTool(env, cli_env, root=config.agent.workspace_root))
    for tool in extensions.tools:
        registry.register(tool)

    def execution_environment(user, call):
        client = Path(__file__).resolve().parents[2] / "scripts/oath-cli"
        return {
            "OATH_CLI_SERVER": config.agent.cli_public_url or config.services.biz_url,
            "OATH_CLI_TOKEN": cli_token(
                config,
                user,
                parent=call["invocation_id"],
                channel=call.get("channel", "unknown"),
                ttl=300,
            ),
            "OATH_CLI_SHA256": hashlib.sha256(client.read_bytes()).hexdigest(),
            "OATH_CLI_INSTALLER_SHA256": hashlib.sha256(
                client.with_name("install-oath-cli").read_bytes()
            ).hexdigest(),
        }

    policy = (
        RemotePermissionPolicy(config.agent.permission_policy_url, token)
        if config.agent.permission_policy_url
        else AllowAllPolicy(storage)
    )
    notifier = (
        PermissionNotifier(config.agent.permission_notification_url, token)
        if config.agent.permission_notification_url
        else None
    )
    tools = ThreadedToolSystem(
        registry,
        DurableEventSink(storage),
        policy,
        approval_notifier=(
            lambda user, payload: (
                notifier.notify(user.user_id, payload, str(payload["invocation_id"]))
                if notifier
                else None
            )
        ),
        execution_environment=execution_environment,
        exposed_tools=config.agent.exposed_tools,
        store=RuntimeStore(storage, patch_commits=True, read_projection="tools"),
        queue=StoredQueue(queue),
        traces=StoredTraces(storage),
        shrinker=ResultShrinker(
            details, config.agent.tool_result_detail_chars, whole=frozenset({DETAIL_READ, SKILL})
        ),
    )
    try:
        serve_rpc(
            ToolService(tools, config),
            token,
            {
                "tools.definitions",
                "tools.submit",
                "tools.permission",
                "tools.cancel",
                "tools.batch",
                "tools.cli_catalog",
                "tools.cli_submit",
                "tools.cli_status",
                "tools.cli_detach",
            },
            config.services.tools_url,
            bind_host=config.services.bind_host,
            service_name="tools",
        )
    finally:
        tools.shutdown()
        if hasattr(policy, "close"):
            policy.close()
        queue.close()
        storage.close()
