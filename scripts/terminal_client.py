"""WS v2 debug console. --json prints the complete wire stream."""

import argparse
import asyncio
import contextlib
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

from websockets.asyncio.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bootstrap.common.config import load_config


class Console:
    def __init__(self, raw=False):
        self.raw = raw
        self.color = sys.stdout.isatty()
        self.streaming = False

    def style(self, text, color="36"):
        return f"\033[{color}m{text}\033[0m" if self.color else text

    def line(self, title, text="", seq=""):
        if self.streaming:
            print()
            self.streaming = False
        clock = datetime.now().strftime("%H:%M:%S")
        print(
            f"{self.style(clock, '90')} {self.style(title):s} {text} {self.style(str(seq), '90')}",
            flush=True,
        )

    def frame(self, frame):
        if self.raw:
            print(json.dumps(frame, ensure_ascii=False), flush=True)
            return
        kind = frame["type"]
        if kind in {"offline.start", "offline.end"}:
            self.line(kind, f"cursor {frame.get('after_cursor', '')} → {frame['through_cursor']}")
        elif kind == "event":
            kind = frame["event_type"]
            p = frame["payload"]
            seq = f"#{frame['event_seq']}"
            if kind == "assistant.delta":
                if not self.streaming:
                    print(self.style("Hymio  ", "34"), end="", flush=True)
                    self.streaming = True
                print(p.get("text", ""), end="", flush=True)
            elif kind == "assistant.message":
                self.line("assistant", p.get("content", "") if not self.streaming else "✓", seq)
                if p.get("tool_calls"):
                    self.line("tools", ", ".join(c["name"] for c in p["tool_calls"]))
            elif kind == "step.status":
                self.line(
                    p["status"],
                    f"step={p['step_id']} trace={p.get('trace_id', '')} "
                    + str(p.get("error") or ""),
                    seq,
                )
            elif kind == "permission.requested":
                self.line(
                    "approval", f"{p['tool']} {json.dumps(p['arguments'], ensure_ascii=False)}", seq
                )
                self.line(
                    "decide", f"/approve {p['invocation_id']}  or  /deny {p['invocation_id']}"
                )
            elif kind == "tool.terminal":
                self.line("tool", f"{p['invocation_id']} → {p['outcome']}", seq)
            else:
                self.line(kind, json.dumps(p, ensure_ascii=False), seq)
        elif kind == "ack":
            if frame.get("user_id"):
                self.line("connected", f"user={frame['user_id']}")
        else:
            self.line(kind, json.dumps(frame, ensure_ascii=False))


async def chat(args):
    config = load_config(args.config)
    matches = [token for token, uid in config.services.users.items() if uid == args.user_id]
    if len(matches) != 1:
        raise SystemExit("Exactly one user credential is required for --user-id")
    credential = matches[0]
    ui = Console(args.json)
    if not args.json:
        ui.line("OATH", f"{args.url}  ·  device={args.client_id}")
        ui.line(
            "commands", "/status /retry /cancel /approve ID /deny ID /quit · --json for raw events"
        )
    async with connect(args.url) as ws:

        async def send(type_, payload):
            await ws.send(
                json.dumps({"v": 2, "id": uuid.uuid4().hex, "type": type_, "payload": payload})
            )

        await send("agent.attach", {"credential": credential, "client_id": args.client_id})

        async def read():
            async for raw in ws:
                frame = json.loads(raw)
                ui.frame(frame)
                if frame["type"] == "event":
                    await send("event.ack", {"cursor": frame["event_seq"]})

        async def write():
            reader = asyncio.StreamReader()
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
            )
            try:
                while line := await reader.readline():
                    text = line.decode().strip()
                    if not text:
                        continue
                    cmd, _, value = text.partition(" ")
                    if cmd == "/quit":
                        return
                    if cmd in {"/approve", "/deny"}:
                        await send(
                            "permission.decide",
                            {
                                "invocation_id": value,
                                "status": "approved" if cmd == "/approve" else "denied",
                            },
                        )
                    elif cmd in {"/status", "/retry", "/cancel"}:
                        await send(
                            {
                                "/status": "agent.status",
                                "/retry": "step.retry",
                                "/cancel": "run.cancel",
                            }[cmd],
                            {},
                        )
                    else:
                        await send("user.message", {"content": text, "user_context": {}})
            finally:
                transport.close()

        tasks = [asyncio.create_task(read()), asyncio.create_task(write())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8765")
    parser.add_argument("--config", default="oath.toml")
    parser.add_argument("--user-id", default="local")
    parser.add_argument("--client-id", default="terminal")
    parser.add_argument("--json", action="store_true")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(chat(parser.parse_args()))
