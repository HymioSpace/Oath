"""WebSocket server：把客户端接进 runtime。

**core 不 import `websockets`。** 这一层是 core 与 socket 之间的桥，两个方向：

- 上行：解析帧 → `Runtime.submit` / `open_session`。两者都是纯内存操作，直接在事件
  循环里调 —— 不落盘，也就没有要等的写操作。**同一条连接上的多条 command 因此保持
  顺序**：交给线程池的话，第二条可能比第一条先到队列。
- 下行：**事件**走 `OutboundHub`（有界队列、有背压）；**控制帧**（ack / pong / error /
  close）直接写在这条连接上 —— 它们是对某一条 command 的回应，跟着那条连接走才对，
  排队没有意义。

会话绑定之后，这条连接不再自称身份：`session_id` 只在 `session.open` 那一次出现，
之后每条 command 的 `session_id` 都要与绑定的那个一致。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Final

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.asyncio.server import serve as _ws_serve

from bootstrap.runtime import Runtime
from core.outbound.hub import ClientChannel
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import (
    CloseReason,
    Command,
    CommandType,
    ack,
    close_frame,
    error_frame,
    pong,
)
from core.protocol.error import WireProtocolError
from core.utils.encoding import canonical_json

_log = logging.getLogger("oath.server")

#: 一帧的最大字节数。command 的 payload 是一句话，不该有大帧。
_MAX_FRAME_BYTES: Final = 1 << 20


class WebSocketChannel(ClientChannel):
    """把一条 websockets 连接包装成 `ClientChannel`。"""

    def __init__(self, connection: ServerConnection) -> None:
        self._connection = connection

    async def send_text(self, text: str) -> None:
        await self._connection.send(text)

    async def close(self, reason: str) -> None:
        """先发一帧 close 让客户端知道为什么，再断。"""
        try:
            await self._connection.send(canonical_json(close_frame(reason)))
        except Exception:
            return
        await self._connection.close()


async def _serve_connection(runtime: Runtime, connection: ServerConnection) -> None:
    scope: Scope | None = None
    channel = WebSocketChannel(connection)
    try:
        async for raw in connection:
            try:
                command = Command.parse(json.loads(raw))
            except (WireProtocolError, ValueError) as exc:
                await _fail(connection, None, exc)
                return

            if command.type == CommandType.SESSION_OPEN:
                if scope is not None:
                    await _fail(
                        connection,
                        command.id,
                        WireProtocolError("这条连接已经绑过 session 了", code="already_bound"),
                    )
                    return
                try:
                    scope = runtime.open_session(command)
                except Exception as exc:
                    await connection.send(
                        canonical_json(close_frame(CloseReason.UNAUTHORIZED, str(exc)))
                    )
                    await connection.close()
                    return
                runtime.hub.attach(scope, channel)
                runtime.ensure_session(scope)
                await connection.send(canonical_json(ack(command.id, session_id=scope.session_id)))
                continue

            if scope is None:
                await _fail(
                    connection,
                    command.id,
                    WireProtocolError("先 session.open", code="not_bound"),
                )
                return
            if command.session_id != scope.session_id:
                await _fail(
                    connection,
                    command.id,
                    WireProtocolError("session_id 与绑定的不是一个", code="session_mismatch"),
                )
                return

            if command.type == CommandType.PING:
                await connection.send(canonical_json(pong(command.id)))
                continue

            try:
                runtime.submit(command, scope)
            except WireProtocolError as exc:
                await _fail(connection, command.id, exc)
            except Exception as exc:  # 装配层给不出更细的分类
                _log.exception("command %s 处理失败", command.type)
                await connection.send(
                    canonical_json(error_frame(command.id, "internal_error", str(exc)))
                )
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if scope is not None:
            runtime.hub.detach(scope, channel)


async def _fail(connection: ServerConnection, command_id: str | None, exc: Exception) -> None:
    """回一帧 error 就断。**帧不合法时不能只回错误继续收** —— 两端的认知已经分叉了。"""
    code = getattr(exc, "code", "internal_error")
    await connection.send(canonical_json(error_frame(command_id, code, str(exc))))
    if command_id is None:
        await connection.send(canonical_json(close_frame(CloseReason.PROTOCOL_ERROR, str(exc))))
        await connection.close()


async def serve(runtime: Runtime, *, host: str, port: int, max_connections: int) -> None:
    """起 server，一直跑到被取消。"""
    active = 0

    async def handler(connection: ServerConnection) -> None:
        nonlocal active
        if active >= max_connections:
            # 直接断，不排队：排在后面等着的是一个已经知道自己连不上的客户端。
            await connection.send(
                canonical_json(close_frame(CloseReason.SLOW_CONSUMER, "连接数已满"))
            )
            await connection.close()
            return
        active += 1
        try:
            await _serve_connection(runtime, connection)
        finally:
            active -= 1

    async with _ws_serve(handler, host, port, max_size=_MAX_FRAME_BYTES, max_queue=16):
        _log.info("Oath 监听 ws://%s:%s（最多 %d 条连接）", host, port, max_connections)
        await asyncio.Future()


__all__ = ["WebSocketChannel", "serve"]
