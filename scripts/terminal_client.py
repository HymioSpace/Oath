#!/usr/bin/env python3
"""终端客户端：连上 Oath，发一句话，把回来的事件打出来。

    uv run python scripts/terminal_client.py --url ws://127.0.0.1:8765 --token demo-token

输入一行发一条 `user.message`；`:quit` 退出。
**验证用的** —— 它不做任何本地状态，界面上看到什么就是 Oath 推过来的。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import json
import sys

import websockets
from websockets import ClientConnection

from core.protocol.datatype.wire import Command, CommandType, FrameType


def _render(raw: str) -> None:
    """把一帧下行消息变成人能看的一行。"""
    frame = json.loads(raw)
    kind = frame.get("type")
    if kind == FrameType.EVENT:
        event_type = frame.get("event_type", "?")
        payload = frame.get("payload", {})
        if event_type == "assistant.message":
            text = payload.get("content") or ""
            if payload.get("tool_calls"):
                names = [c.get("name") for c in payload["tool_calls"]]
                text = f"{text} [要调 {', '.join(names)}]".strip()
            print(f"  assistant: {text}")
        elif event_type == "tool.call":
            args = json.dumps(payload.get("arguments"), ensure_ascii=False)
            print(f"  tool.call: {payload.get('tool')}({args})")
        elif event_type == "tool.result":
            outcome = payload.get("outcome")
            detail = payload.get("content")
            if detail is None:
                detail = payload.get("error")
            print(f"  tool.result[{outcome}]: {detail}")
        else:
            print(f"  {event_type}: {json.dumps(payload, ensure_ascii=False)}")
    elif kind == FrameType.ACK:
        print(f"  ack: session={frame.get('session_id')}")
    elif kind == FrameType.PONG:
        print("  pong")
    elif kind == FrameType.ERROR:
        print(f"  error[{frame.get('code')}]: {frame.get('message')}", file=sys.stderr)
    elif kind == FrameType.CLOSE:
        print(f"  close[{frame.get('reason')}]: {frame.get('message')}", file=sys.stderr)


def _as_text(raw: str | bytes) -> str:
    return raw.decode() if isinstance(raw, bytes) else raw


async def _read_loop(websocket: ClientConnection) -> None:
    async for raw in websocket:
        _render(_as_text(raw))


async def _run(url: str, token: str) -> None:
    async with websockets.connect(url, max_size=1 << 20) as websocket:
        opener = Command(
            id="c-open",
            type=CommandType.SESSION_OPEN,
            session_id=None,
            payload={"credential": token},
        )
        await websocket.send(json.dumps(opener.to_json(), ensure_ascii=False))
        first = _as_text(await websocket.recv())
        _render(first)
        frame = json.loads(first)
        if frame.get("type") != FrameType.ACK:
            return
        session_id = frame["session_id"]

        reader = asyncio.create_task(_read_loop(websocket))
        ids = itertools.count(1)
        print(f"连上了（session={session_id}）。输入一句话开始聊，:quit 退出。")
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, input, "你 > ")
            if line.strip() in (":quit", ":q"):
                break
            if not line.strip():
                continue
            command = Command(
                id=f"c-{next(ids)}",
                type=CommandType.USER_MESSAGE,
                session_id=session_id,
                payload={"content": line},
            )
            await websocket.send(json.dumps(command.to_json(), ensure_ascii=False))
        reader.cancel()


def main() -> None:
    parser = argparse.ArgumentParser(description="Oath 终端客户端")
    parser.add_argument("--url", default="ws://127.0.0.1:8765")
    parser.add_argument("--token", default="demo-token")
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args.url, args.token))


if __name__ == "__main__":
    main()
