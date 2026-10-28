"""Execution client: typed arguments, authoritative status and safe transport."""

import importlib.machinery
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest


def load_client():
    loader = importlib.machinery.SourceFileLoader(
        "execution_cli", str(Path(__file__).parents[1] / "scripts/oath-cli")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_structured_args_and_original_submission_id(capsys):
    cli = load_client()
    seen = []

    def call(path, payload=None):
        seen.append((path, payload))
        if path.endswith("invoke"):
            return {"invocation_id": "child"}
        return {"status": "succeeded", "result": {"content": {"ok": True}}}

    with patch.object(cli, "call", side_effect=call):
        assert (
            cli.main(
                [
                    "tasks",
                    "--action",
                    "create",
                    "--json",
                    '{"schedule":{"mode":"once"}}',
                    "--request-id",
                    "stable",
                ]
            )
            == 0
        )
    assert seen[0][1]["arguments"] == {"action": "create", "schedule": {"mode": "once"}}
    assert seen[0][1]["request_id"] == "stable"
    assert seen[1] == ("/cli/status", {"invocation_id": "child"})
    assert json.loads(capsys.readouterr().out)["result"]["content"]["ok"]


def test_async_does_not_wait_or_repeat():
    cli = load_client()
    with patch.object(cli, "call", return_value={"invocation_id": "child"}) as call:
        assert cli.main(["image_generate", "--async", "--prompt", "an otter"]) == 0
    assert call.call_count == 1
    assert call.call_args.args[1]["async"] is True


def test_expired_wait_detaches_instead_of_losing_completion():
    cli = load_client()
    with patch.object(cli, "call", return_value={"invocation_id": "child"}) as call:
        cli.main(["tasks", "--action", "list", "--wait", "0"])
    assert call.call_args_list[-1].args == ("/cli/detach", {"invocation_id": "child"})


def test_failure_and_approval_are_not_success(capsys):
    cli = load_client()
    for status in ("failed", "timeout", "outcome_unknown", "waiting_approval"):
        effects = [{"invocation_id": "child"}, {"status": status}]
        if status == "waiting_approval":
            effects.append({"status": "detached"})
        with patch.object(
            cli, "call", side_effect=effects
        ):
            assert cli.main(["tasks", "--action", "list"]) == 1


def test_device_transport_rejects_cleartext_and_embedded_credentials(monkeypatch):
    cli = load_client()
    for url in (
        "http://remote.example",
        "https://user:pass@example.com",
        "https://example.com?token=secret",
    ):
        monkeypatch.setenv("OATH_CLI_SERVER", url)
        with pytest.raises(ValueError):
            cli.call("/cli/tools")
