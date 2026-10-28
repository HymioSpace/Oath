import hashlib
import importlib.machinery
import importlib.util
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from bootstrap.biz.server import build_app
from bootstrap.biz.tool_cli import routes
from bootstrap.common.authentication import cli_claims, cli_token
from bootstrap.tools.process import ToolService
from core.journal.runtime_store import RuntimeStore
from core.protocol.datatype import (
    ToolCallRequest,
    ToolDefinition,
    ToolInvocation,
    ToolResult,
    User,
)
from core.protocol.error import PermissionDeniedError, ToolExecutionError
from core.protocol.interface import Tool
from core.shell.cli_tool import CliTool
from core.storage.adapters import DurableEventSink, StoredQueue, StoredTraces
from core.tool.impl import ThreadedToolSystem
from core.tool.registry import ToolRegistry


class Parent(Tool):
    def __init__(self, stop):
        self.stop = stop

    @property
    def definition(self):
        return ToolDefinition("bash", "fixture", {"type": "object"})

    def execute(self, invocation):
        self.stop.wait(5)
        return ToolResult({"done": True})


class Probe(Tool):
    @property
    def definition(self):
        return ToolDefinition("probe", "fixture", {"type": "object"})

    def execute(self, invocation):
        return ToolResult({"answer": 42})


def wait_for(fn):
    until = time.monotonic() + 3
    while time.monotonic() < until:
        result = fn()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("Timed out")


def test_cli_children_use_tool_system_and_do_not_wake_per_command(service):
    stop = threading.Event()
    registry = ToolRegistry(default_timeout_seconds=10)
    registry.register(Parent(stop))
    registry.register(Probe())
    user = User("synthetic-cli")
    # Tool permissions normally wrap a PermissionPolicy; provide its interface.
    from core.protocol.interface.permission import PermissionPolicy

    class Allow(PermissionPolicy):
        def check(self, *args):
            return "allow"

    tools = ThreadedToolSystem(
        registry,
        DurableEventSink(service),
        Allow(),
        store=RuntimeStore(service, patch_commits=True),
        queue=StoredQueue(service),
        traces=StoredTraces(service),
        exposed_tools=("bash", "skill", "cli"),
    )
    config = SimpleNamespace(
        services=SimpleNamespace(internal_token="synthetic-internal-token-" * 3)
    )
    api = ToolService(tools, config)
    try:
        tools.submit_batch(
            user,
            "origin",
            [ToolCallRequest("bash", {}, "parent", "parent", "origin", "run", "web")],
        )
        wait_for(lambda: tools.status(user, "parent").status == "running")
        token = cli_token(config, user, parent="parent", channel="web")
        result = api.call(
            "tools.cli_submit", credential=token, name="probe", arguments={}, request_id="same"
        )
        child = result["invocation_id"]
        wait_for(lambda: tools.status(user, child).status == "succeeded")
        assert api.call("tools.cli_status", credential=token, invocation_id=child)["result"][
            "content"
        ] == {"answer": 42}
        again = api.call(
            "tools.cli_submit", credential=token, name="probe", arguments={}, request_id="same"
        )
        assert again["invocation_id"] == child
        with service.transaction() as db:
            assert db.execute("select count(*) from broker_outbox").fetchone()[0] == 0
        assert [d.name for d in tools.definitions()] == ["bash"]
        api.call("tools.cli_detach", credential=token, invocation_id=child)
        with service.transaction() as db:
            assert db.execute("select count(*) from broker_outbox").fetchone()[0] == 1
        other = cli_token(config, User("other"), parent="parent")
        assert api.call("tools.cli_status", credential=other, invocation_id=child) == {
            "status": "not_found"
        }
        with pytest.raises(ValueError):
            api.call(
                "tools.cli_submit", credential=other, name="probe", arguments={}, request_id="x"
            )
    finally:
        stop.set()
        tools.shutdown()


def test_cli_capability_signature_expiry_and_identity():
    config = SimpleNamespace(services=SimpleNamespace(internal_token="synthetic-private-token" * 3))
    token = cli_token(config, User("owner"), parent="parent", channel="ios")
    assert cli_claims(config, token)["channel"] == "ios"
    with pytest.raises(PermissionDeniedError):
        cli_claims(config, token + "broken")
    with (
        patch("bootstrap.common.authentication.time.time", return_value=time.time() + 1000),
        pytest.raises(PermissionDeniedError),
    ):
        cli_claims(config, token)


def test_public_assets_contain_no_credentials_and_calls_require_capability():
    config = SimpleNamespace(services=SimpleNamespace(internal_token="synthetic-private-token" * 3))

    class Remote:
        def call(self, *args, **kwargs):
            raise AssertionError("Must not dispatch")

    client = TestClient(Starlette(routes=routes(config, Remote())))
    assert client.get("/cli/tools").status_code == 401
    data = client.get("/cli/client").content
    assert hashlib.sha256(data).hexdigest() == client.get("/cli/manifest").json()["sha256"]
    assert config.services.internal_token.encode() not in data


def test_static_tool_cli_routes_precede_business_command_catch_all():
    config = SimpleNamespace(services=SimpleNamespace(internal_token="synthetic-private-token" * 3))

    class Service:
        def list_commands(self):
            return ()

    class Remote:
        def call(self, *args, **kwargs):
            raise AssertionError("Must not dispatch")

    client = TestClient(
        build_app(
            Service(),
            Service(),
            additional_routes=routes(config, Remote()),
        )
    )
    response = client.post("/cli/status", json={"invocation_id": "probe"})
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


def test_cli_nonzero_exit_is_a_failure(tmp_path):
    client = tmp_path / "oath-cli"
    client.write_text(
        "import sys\nprint('CLI request failed: HTTPError', file=sys.stderr)\nraise SystemExit(2)\n"
    )
    tool = CliTool(client)
    invocation = ToolInvocation(
        User("cli-failure"),
        "cli-failure",
        {"command": "status missing"},
        time.monotonic() + 5,
    )
    with pytest.raises(ToolExecutionError, match="CLI failed"):
        tool.execute(invocation)


def test_installer_verifies_before_replacing_and_keeps_credentials_out(tmp_path):
    import io

    loader = importlib.machinery.SourceFileLoader(
        "installer", str(Path(__file__).parents[1] / "scripts/install-oath-cli")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    target = tmp_path / "bin"
    target.mkdir(mode=0o700)
    old = target / "oath-cli"
    old.write_text("previous-client")
    content = b"#!/usr/bin/env python3\nprint('fixture')\n"
    with patch.object(
        module,
        "build_opener",
        return_value=SimpleNamespace(open=lambda *a, **k: io.BytesIO(content)),
    ):
        with pytest.raises(ValueError, match="integrity"):
            module.install("https://example.com/cli/client", "0" * 64, target)
        assert old.read_text() == "previous-client"
        installed = module.install(
            "https://example.com/cli/client", hashlib.sha256(content).hexdigest(), target
        )
    assert installed.read_bytes() == content
    assert installed.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ValueError):
        module.install(
            "http://remote.example/cli/client", hashlib.sha256(content).hexdigest(), target
        )


def test_admin_prompt_matches_the_worker_builder_and_requires_admin_token(tmp_path):
    from bootstrap.biz.prompt_preview import prompt_routes
    from core.agent.prompt import system_prompt
    from core.protocol.datatype.config import AgentConfig
    from core.skill import SkillSystem

    config = SimpleNamespace(
        agent=AgentConfig(
            system_prompt="# Synthetic role\nConcise assistant.",
            builtin_bash=False,
            extension_dirs=(),
            skill_dir=tmp_path,
            skill_dirs=(),
            skill_scan_dirs=(),
        ),
        services=SimpleNamespace(internal_token="synthetic-internal-token-" * 3),
    )
    client = TestClient(Starlette(routes=prompt_routes(config)))
    assert client.get("/admin/system-prompt").status_code == 401
    response = client.get(
        "/admin/system-prompt?channel=ios",
        headers={"Authorization": "Bearer " + config.services.internal_token},
    )
    assert response.status_code == 200
    expected = system_prompt(config.agent, SkillSystem((tmp_path,)), "ios")
    assert response.json()["markdown"] == expected
    assert response.json()["sha256"] == hashlib.sha256(expected.encode()).hexdigest()
    assert not response.json()["includes_personal_context"]
