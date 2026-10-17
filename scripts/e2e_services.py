"""Real processes, RabbitMQ, SQLite, WS and HTTP; deterministic HTTP model stub.

Run with --amqp-url against a disposable RabbitMQ instance. No real model credentials.
Runtime data and logs stay in a temporary directory, printed on completion.
"""

import argparse
import asyncio
import contextlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import httpx
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bootstrap.clients.service_client import ServiceClient  # noqa: E402


class Model(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: ClassVar[list] = []
    lock = threading.Lock()

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.lock:
            self.requests.append(body)
        if not body.get("stream"):
            response = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                "<context_digest><summary>Earlier conversation retained."
                                "</summary><facts/><open/></context_digest>"
                            )
                            },
                            "finish_reason": "stop",
                        }
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return
        content = next(
            (m["content"] for m in reversed(body["messages"]) if m["role"] == "user"), ""
        )
        if "slow" in content:
            time.sleep(2)
        has_result = any(m["role"] == "tool" for m in body["messages"])
        if "bash" in content and not has_result:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-smoke",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": json.dumps({"command": "printf oath-e2e"}),
                        },
                    }
                ]
            }
            finish = "tool_calls"
        else:
            delta = {"content": "ok:" + content}
            finish = "stop"
        chunks = [
            json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}),
            json.dumps(
                {
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                }
            ),
            "[DONE]",
        ]
        response = "".join("data: " + c + "\n\n" for c in chunks).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError):
            self.wfile.write(response)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def eventually(fn, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await asyncio.to_thread(fn)
        if result:
            return result
        await asyncio.sleep(0.1)
    raise AssertionError("Timed out waiting for durable state")


async def exercise(ports, storage, token, children, launch, broker_container=None):
    url = f"ws://127.0.0.1:{ports['gateway']}"

    def state(uid="alice"):
        return storage.call("state.read", user_id=uid)["state"]

    async def command(ws, type_, payload, identity=None):
        identity = identity or uuid.uuid4().hex
        await ws.send(json.dumps({"v": 2, "id": identity, "type": type_, "payload": payload}))
        return identity

    async def attach(client="device-a", credential=token):
        ws = await connect(url)
        await command(ws, "agent.attach", {"credential": credential, "client_id": client})
        frames = []
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), 10))
            frames.append(frame)
            if frame["type"] == "offline.end":
                break
        return ws, frames

    ws, frames = await attach()
    assert [f["type"] for f in frames] == ["ack", "offline.start", "offline.end"]
    await command(
        ws, "user.message", {"content": "slow first", "user_context": {"locale": "zh"}}, "first"
    )
    await eventually(lambda: any(s["attempts"] for s in state()["steps"].values()))
    await command(ws, "user.message", {"content": "second", "user_context": {}}, "second")
    await ws.close()
    await eventually(
        lambda: len([s for s in state()["steps"].values() if s["status"] == "completed"]) == 2
    )
    assert len(state()["inputs"]) == 2
    print("PASS: offline execution + new input during active step", flush=True)
    ws, replay = await attach()
    events = [f for f in replay if f["type"] == "event"]
    assert (
        len(events) > 0
        and replay[1]["type"] == "offline.start"
        and replay[-1]["type"] == "offline.end"
    )
    assert [f["event_seq"] for f in events] == list(range(1, events[-1]["event_seq"] + 1))
    high = events[-1]["event_seq"]
    await command(ws, "event.ack", {"cursor": high})
    await command(ws, "ping", {})
    while json.loads(await ws.recv())["type"] != "pong":
        pass
    await ws.close()
    ws, replay = await attach()
    assert not [f for f in replay if f["type"] == "event"]
    await command(ws, "user.message", {"content": "second", "user_context": {}}, "second")
    await asyncio.sleep(0.5)
    assert len(state()["inputs"]) == 2
    await ws.close()
    ws, replay = await attach("device-b")
    assert any(f["type"] == "event" for f in replay)
    await ws.close()
    print("PASS: replay boundaries + ACK + device cursors + input dedup", flush=True)
    ws, _ = await attach("bob-device", token + "b")
    await command(ws, "user.message", {"content": "bash please", "user_context": {}})
    await eventually(
        lambda: len([s for s in state("bob")["steps"].values() if s["status"] == "completed"]) == 2
    )
    call = next(iter(state("bob")["calls"].values()))
    assert call["status"] == "succeeded" and "oath-e2e" in call["terminal"]["content"]
    await ws.close()
    assert len(state()["inputs"]) == 2
    with httpx.Client(trust_env=False) as http:
        traces = http.get(
            f"http://127.0.0.1:{ports['biz']}/list_traces",
            headers={"Authorization": "Bearer " + token + "b"},
        ).json()
        assert traces["total"]
        trace = http.get(
            f"http://127.0.0.1:{ports['biz']}/query_trace",
            params={"trace_id": traces["trace_ids"][0]["trace_id"]},
            headers={"Authorization": "Bearer " + token + "b"},
        ).json()
        assert trace["spans"]
    print("PASS: tool round trip + user isolation + Biz trace API", flush=True)
    ws, _ = await attach()
    await command(ws, "user.message", {"content": "slow recover", "user_context": {}})
    await eventually(
        lambda: any(s["status"] == "prepared" and s["attempts"] for s in state()["steps"].values())
    )
    children["worker"].kill()
    children["worker"].wait()
    children["worker"] = launch("worker")
    await ws.close()
    await eventually(
        lambda: len([s for s in state()["steps"].values() if s["status"] == "completed"]) == 3
    )
    print("PASS: worker crash and lease-based recovery", flush=True)
    # Storage restart preserves user state, cursor and pending-work identity.
    children["storage"].kill()
    children["storage"].wait()
    children["storage"] = launch("storage")
    await asyncio.sleep(1)
    assert len(state()["steps"]) == 3
    ws, replay = await attach()
    assert any(f["type"] == "event" for f in replay)
    await ws.close()
    print("PASS: Storage restart preserves state and replay", flush=True)

    # A stale worker cannot complete a different owner's claimed revision.
    old = storage.call(
        "lease.acquire", user_id="fence-probe", lane="compaction", owner="old", ttl=0.05
    )
    await asyncio.sleep(0.1)
    new = storage.call(
        "lease.acquire", user_id="fence-probe", lane="compaction", owner="new", ttl=3
    )
    try:
        storage.call("outbox.append", user_id="fence-probe", type="probe", payload={}, fence=old)
    except Exception as exc:
        assert type(exc).__name__ == "LeaseLost"
    else:
        raise AssertionError("Stale lease was accepted")
    storage.call("lease.release", user_id="fence-probe", fence=new)
    print("PASS: stale lease writes rejected", flush=True)

    if broker_container:
        await asyncio.to_thread(
            subprocess.run, ["docker", "stop", broker_container], check=True, capture_output=True
        )
        ws, _ = await attach()
        identity = await command(
            ws, "user.message", {"content": "broker recovery", "user_context": {}}
        )
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if frame.get("type") == "ack" and frame.get("id") == identity:
                break
        await ws.close()
        await asyncio.to_thread(
            subprocess.run, ["docker", "start", broker_container], check=True, capture_output=True
        )
        await eventually(
            lambda: len([s for s in state()["steps"].values() if s["status"] == "completed"]) == 4,
            timeout=60,
        )
        print("PASS: broker outage retains accepted message; recovery delivers it", flush=True)

    ws, _ = await attach("compression-device", token + "c")
    for n in range(3):
        await command(
            ws,
            "user.message",
            {"content": "compact " + "x" * 6500, "user_context": {}},
            f"compact-{n}",
        )
        await eventually(
            lambda n=n: (
                len([s for s in state("charlie")["steps"].values() if s["status"] == "completed"])
                == n + 1
            )
        )
    await eventually(lambda: bool(state("charlie")["compactions"].get("applied")))
    await ws.close()
    print(
        "PASS: context compaction survives step turnover and applies under Brain lease", flush=True
    )

    before = len(Model.requests)
    storage.call("supervisor.tick", interval=0.1)
    await asyncio.sleep(1)
    assert len(Model.requests) == before
    print("PASS: idle supervisor does not call model", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--amqp-url", default=os.environ.get("OATH_AMQP_URL"))
    parser.add_argument(
        "--broker-container", help="Disposable Docker broker to stop/start during verification"
    )
    args = parser.parse_args()
    if not args.amqp_url:
        raise SystemExit("Provide --amqp-url for a disposable RabbitMQ instance")
    root = Path(tempfile.mkdtemp(prefix="oath-e2e-"))
    ports = {
        s: free_port() for s in ("storage", "queue", "manager", "tools", "gateway", "biz", "model")
    }
    token = secrets.token_urlsafe(32)
    internal = secrets.token_urlsafe(32)
    env = {
        **os.environ,
        "E2E_UNUSED_KEY": "local-stub",
        "OATH_SERVICE_TOKEN": internal,
        "OATH_USERS": json.dumps({token: "alice", token + "b": "bob", token + "c": "charlie"}),
        "OATH_AMQP_URL": args.amqp_url,
    }
    config = root / "oath.toml"
    config.write_text(
        f'data_root = "{root}/data"\n[services]\n'
        + "".join(
            f'{s}_url="http://127.0.0.1:{ports[s]}"\n'
            for s in ("storage", "queue", "manager", "tools", "biz")
        )
        + f'gateway_port={ports["gateway"]}\nbiz_port={ports["biz"]}\nlease_seconds=6\nrequest_timeout=2\npoll_seconds=0.1\nworkers=2\n[agent]\nmax_output_tokens=512\ncontext_window=8000\ncompress_keep_recent=2\ncompress_summary_tokens=64\nextension_dirs=[]\nskill_dir="{root}/skills"\nskill_scan_dirs=[]\nworkspace_root="{root}"\ncli_bin_dir="{ROOT}/scripts"\n[agent.provider]\nmodel="e2e"\napi_base="http://127.0.0.1:{ports["model"]}/v1"\napi_key_env="E2E_UNUSED_KEY"\ntimeout_seconds=10\n[supervisor]\ninvoke_interval_seconds=2\n'
    )
    model = ThreadingHTTPServer(("127.0.0.1", ports["model"]), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    children = {}
    files = []

    def launch(name):
        log = (root / f"{name}.log").open("ab")
        files.append(log)
        return subprocess.Popen(
            [sys.executable, "-m", "bootstrap", name, "--config", str(config)],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    try:
        for name in (
            "storage",
            "queue",
            "manager",
            "tools",
            "consumer",
            "worker",
            "gateway",
            "biz",
            "supervisor",
        ):
            children[name] = launch(name)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with (
                    socket.create_connection(("127.0.0.1", ports["gateway"]), timeout=0.2),
                    socket.create_connection(("127.0.0.1", ports["biz"]), timeout=0.2),
                ):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            raise AssertionError("Services failed startup")
        with_client = ServiceClient(f"http://127.0.0.1:{ports['storage']}", internal, 2)
        asyncio.run(exercise(ports, with_client, token, children, launch, args.broker_container))
        with_client.close()
    finally:
        for child in children.values():
            child.terminate()
        for child in children.values():
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for f in files:
            f.close()
        model.shutdown()
        print("Artifacts:", root)


if __name__ == "__main__":
    main()
