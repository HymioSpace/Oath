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
import json
import sys
from uuid import uuid4

import websockets
from websockets import ClientConnection

from core.protocol.datatype.wire import Command, CommandType, FrameType


class _DeltaLine:
    """正在往同一行上接增量的那一行。

    换 `step_id`（新一轮）或换 `channel`（从思考切到作答）就换行 —— 两路混在
    一行里看不清哪段是答案。
    """

    def __init__(self) -> None:
        self._open: tuple[str, str] | None = None

    def add(self, payload: dict[str, object]) -> None:
        key = (str(payload.get("step_id", "")), str(payload.get("channel", "content")))
        if self._open != key:
            self._break()
            self._open = key
            label = "  think: " if key[1] == "reasoning" else "  assistant: "
            print(label, end="", flush=True)
        print(str(payload.get("text", "")), end="", flush=True)

    def close(self) -> None:
        """有行开着才换行。没有开过就不打空行。"""
        self._break()

    def _break(self) -> None:
        if self._open is not None:
            print()
        self._open = None


#: 一条连接一个。终端客户端只有一条连接，不需要按 session 分。
_delta = _DeltaLine()


def _render(raw: str) -> None:
    """把一帧下行消息变成人能看的一行。

    **增量不另起一行。** 一段一段往当前行后面接，完整的 `assistant.message` 到了
    再补一个换行 —— 客户端看到的是「正在打字的回答」，不是一个字一个字刷屏。
    """
    frame = json.loads(raw)
    kind = frame.get("type")
    if kind == FrameType.EVENT:
        event_type = frame.get("event_type", "?")
        payload = frame.get("payload", {})
        if event_type == "assistant.delta":
            _delta.add(payload)
        elif event_type == "assistant.message":
            _delta.close()
            text = payload.get("content") or ""
            if payload.get("tool_calls"):
                names = [c.get("name") for c in payload["tool_calls"]]
                text = f"{text} [要调 {', '.join(names)}]".strip()
            print(f"  assistant: {text}")
        elif event_type == "tool.call":
            _delta.close()
            args = json.dumps(payload.get("arguments"), ensure_ascii=False)
            print(f"  tool.call: {payload.get('tool')}({args})")
        elif event_type == "tool.result":
            _delta.close()
            outcome = payload.get("outcome")
            detail = payload.get("content")
            if detail is None:
                detail = payload.get("error")
            print(f"  tool.result[{outcome}]: {detail}")
        else:
            _delta.close()
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


def _command_id() -> str:
    """一条 command 的 id —— **它就是幂等键**。

    所以不能是「这条连接里的第几条」：重连后 session 还在、journal 还在，从 1 重新
    编号会让新消息撞上旧消息的键，journal 只能按「同键异 payload」抛错。
    它得在整个 session 内唯一。真正的重发要复用同一个 id —— 那是去重，不是新消息。
    """
    return f"c-{uuid4().hex[:12]}"


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
        print(f"连上了（session={session_id}）。输入一句话开始聊，:quit 退出。")
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, input, "你 > ")
            if line.strip() in (":quit", ":q"):
                break
            if not line.strip():
                continue
            command = Command(
                id=_command_id(),
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
