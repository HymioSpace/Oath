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
    env = root / ".env"
    env.write_text(
        "\n".join(
            [
                "OATH_SERVICE_TOKEN=" + secrets.token_hex(24),
                "OATH_USERS=" + json.dumps({token: "docker-user"}),
                "OPENAI_API_KEY=local-stub",
                "RABBITMQ_USER=oath",
                "RABBITMQ_PASSWORD=" + secrets.token_hex(24),
                f"OATH_WS_PORT={ws_port}",
                f"OATH_HTTP_PORT={http_port}",
            ]
        )
        + "\n"
    )
    env.chmod(0o600)
    config = (ROOT / "oath.example.toml").read_text()
    config = config.replace('model = "gpt-4o-mini"', 'model = "e2e"')
    config = config.replace(
        'api_base = "https://api.openai.com/v1"',
        f'api_base = "http://host.docker.internal:{model_port}/v1"',
    )
    (root / "oath.toml").write_text(config)
    spec = yaml.safe_load((ROOT / "compose.yaml").read_text())
    spec.pop("x-service", None)
    for service in spec["services"].values():
        if "build" in service:
            service["build"] = str(ROOT)
        if "env_file" in service:
            service["env_file"] = str(env)
        if "volumes" in service:
            service["volumes"] = [
                str(root / "oath.toml") + ":/app/oath.toml:ro"
                if volume == "./oath.toml:/app/oath.toml:ro" else volume
                for volume in service["volumes"]
            ]
    compose = root / "compose.yaml"
    compose.write_text(yaml.safe_dump(spec))
    project = "oath-e2e-" + str(os.getpid())
    cmd = ["docker", "compose", "--env-file", str(env), "-p", project, "-f", str(compose)]
    model = ThreadingHTTPServer(("0.0.0.0", model_port), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    try:
        subprocess.run([*cmd, "up", "-d", "--no-build", "--pull", "never"], check=True)
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
            subprocess.run([*cmd, "logs", "--no-color"], stdout=f, stderr=subprocess.STDOUT)
        subprocess.run([*cmd, "down", "--volumes", "--remove-orphans"], check=True)
        model.shutdown()
        print("Artifacts:", root)


if __name__ == "__main__":
    main()
