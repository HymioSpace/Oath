"""Verify Compose with isolated volumes/ports and a local HTTP model stub."""

import asyncio
import json
import os
import secrets
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import yaml
from e2e_services import Model, free_port
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]


async def verify(ws_port, http_port, token):
    async def send(ws, kind, payload):
        identity = uuid.uuid4().hex
        await ws.send(json.dumps({"v": 2, "type": kind, "id": identity, "payload": payload}))
        return identity

    async with connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await send(ws, "agent.attach", {"credential": token, "client_id": "docker-debug"})
        while json.loads(await asyncio.wait_for(ws.recv(), 10))["type"] != "offline.end":
            pass
        cmd = await send(
            ws, "user.message", {"content": "bash please", "user_context": {"locale": "zh-CN"}}
        )
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if frame.get("type") == "ack" and frame.get("id") == cmd:
                break
    with httpx.Client(trust_env=False) as http:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            listing = http.get(
                f"http://127.0.0.1:{http_port}/list_traces",
                headers={"Authorization": "Bearer " + token},
            ).json()
            if listing.get("trace_ids"):
                trace = http.get(
                    f"http://127.0.0.1:{http_port}/query_trace",
                    params={"trace_id": listing["trace_ids"][0]["trace_id"]},
                    headers={"Authorization": "Bearer " + token},
                ).json()
                steps = [s for s in trace["spans"] if s["parent_span_id"] is None]
                if len(steps) == 2 and all(s["end_unix_nano"] for s in steps):
                    break
            await asyncio.sleep(0.2)
        else:
            raise AssertionError("Docker agent did not finish its tool round trip")
    async with connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await send(ws, "agent.attach", {"credential": token, "client_id": "docker-debug"})
        frames = []
        while True:
            f = json.loads(await asyncio.wait_for(ws.recv(), 10))
            frames.append(f)
            if f["type"] == "offline.end":
                break
        events = [f for f in frames if f["type"] == "event"]
        assert frames[1]["type"] == "offline.start"
        assert any(
            f["event_type"] == "tool.terminal" and f["payload"]["outcome"] == "succeeded"
            for f in events
        )
        assert len([f for f in events if f["event_type"] == "assistant.message"]) == 2
        assert [f["event_seq"] for f in events] == list(range(1, events[-1]["event_seq"] + 1))
    print(
        "PASS: Docker Compose → WS → RabbitMQ → Consumer → Manager → Worker "
        "→ Bash → trace → offline replay",
        flush=True,
    )


def main():
    root = Path(tempfile.mkdtemp(prefix="oath-docker-e2e-"))
    token = secrets.token_urlsafe(32)
    model_port, ws_port, http_port = free_port(), free_port(), free_port()
    vault_port = free_port()
    rabbit_password = secrets.token_hex(24)
    internal = secrets.token_hex(24)
    config = (ROOT / "oath.example.toml").read_text()
    config = config.replace("gateway_port = 8765", f"gateway_port = {ws_port}")
    config = config.replace("biz_port = 8766", f"biz_port = {http_port}")
    config = config.replace('model = "gpt-4o-mini"', 'model = "e2e"')
    config = config.replace(
        'api_base = "https://api.openai.com/v1"',
        f'api_base = "http://host.docker.internal:{model_port}/v1"',
    )
    config = config.replace('credential = "model/openai"', 'credential = "model/e2e"')
    config = config.replace('import_env = "OPENAI_API_KEY"', 'import_env = "E2E_UNUSED_KEY"')
    config = config.replace('internal_token = ""', f'internal_token = "{internal}"')
    config = config.replace("users = {}", f'users = {{ "{token}" = "docker-user" }}')
    config = config.replace('rabbitmq_password = ""', f'rabbitmq_password = "{rabbit_password}"')
    config = config.replace(
        'vault_url = "http://127.0.0.1:8200"',
        f'vault_url = "http://127.0.0.1:{vault_port}"',
    )
    (root / "oath.toml").write_text(config)
    spec = yaml.safe_load((ROOT / "compose.yaml").read_text())
    spec.pop("x-service", None)
    for service in spec["services"].values():
        if "build" in service:
            service["build"] = str(ROOT)
        if "volumes" in service:
            service["volumes"] = [
                str(ROOT / "deploy/vault.hcl") + ":/vault/config/vault.hcl:ro"
                if volume == "./deploy/vault.hcl:/vault/config/vault.hcl:ro"
                else volume
                for volume in service["volumes"]
            ]
    compose = root / "compose.yaml"
    compose.write_text(yaml.safe_dump(spec))
    project = "oath-e2e-" + str(os.getpid())
    cmd = ["docker", "compose", "-p", project, "-f", str(compose)]
    launcher = [
        os.sys.executable,
        "-m",
        "bootstrap.docker",
        "--config",
        str(root / "oath.toml"),
        "--compose",
        str(compose),
        "--project",
        project,
    ]
    env = {**os.environ, "E2E_UNUSED_KEY": "local-stub"}
    compose_env = {
        **env,
        "RABBITMQ_USER": "oath",
        "RABBITMQ_PASSWORD": rabbit_password,
        "OATH_WS_PORT": str(ws_port),
        "OATH_HTTP_PORT": str(http_port),
        "OATH_VAULT_PORT": str(vault_port),
    }
    model = ThreadingHTTPServer(("0.0.0.0", model_port), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    try:
        subprocess.run([*launcher[:3], "up", *launcher[3:], "--no-build"], check=True, env=env)
        tool_env = subprocess.run(
            ["docker", "exec", project + "-tools-1", "env"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "E2E_UNUSED_KEY=" not in tool_env
        mounts = json.loads(
            subprocess.run(
                ["docker", "inspect", project + "-tools-1", "--format", "{{json .Mounts}}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        assert not any("/run/oath/" in mount["Destination"] for mount in mounts)
        denied = httpx.get(
            f"http://127.0.0.1:{vault_port}/v1/oath/data/model/e2e",
            timeout=3,
            trust_env=False,
        )
        assert denied.status_code == 403
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"http://127.0.0.1:{http_port}/heartbeat", trust_env=False, timeout=1)
                gateway = httpx.get(
                    f"http://127.0.0.1:{ws_port}/heartbeat", trust_env=False, timeout=1
                )
                if r.status_code == gateway.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            raise AssertionError("Gateway and Biz did not become ready")
        asyncio.run(verify(ws_port, http_port, token))
    finally:
        with (root / "services.log").open("w") as f:
            subprocess.run(
                [*cmd, "logs", "--no-color"],
                stdout=f,
                stderr=subprocess.STDOUT,
                env=compose_env,
            )
        subprocess.run([*launcher[:3], "down", *launcher[3:], "--volumes"], check=True, env=env)
        model.shutdown()
        print("Artifacts:", root)


if __name__ == "__main__":
    main()
