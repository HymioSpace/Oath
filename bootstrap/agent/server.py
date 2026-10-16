"""WebSocket 到 runtime 的桥。

同一连接按顺序 await submit；fsync 放在线程中，成功后回 ACK。
在线事件经 runtime 的 `OutboundHub` 投递，断线后用 session.status 查询当前持久化
状态。session 绑定后不允许客户端重新声明身份。

`/heartbeat` 是给 supervisor 与 manager 的**专用**连接：连上就收到一帧 alive 与当前
在线连接数，不发 command、不绑 session，也不占连接配额，所以它不会顶掉任何业务连接。
manager 靠这个连接数判断 scope 还有没有人用。

它探的是**进程**，不是健康。答得出「accept 与写帧这条路还通」，答不出消费线程与 Brain
是否还活着 —— 那几样坏了，这个进程一样回心跳。

只读查询不在这里 —— 走 biz 的 HTTP server。查询不是 command。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Final

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.asyncio.server import serve as _ws_serve

from bootstrap.agent.service import SessionService
from core.outbound import ClientChannel
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

#: 探测连接走的 path。业务连接走默认路径，两者由这个区分。
_HEARTBEAT_PATH: Final = "/heartbeat"


async def _serve_heartbeat(connection: ServerConnection, service: SessionService) -> None:
    """专用探测连接：回一帧 alive 与连接数就断。

    **不绑 session、不改任何状态、不占连接配额。** 它必须走完 server 的连接处理循环
    —— 一个旁路线程报 alive，证明不了 accept 与写帧这条路还通。

    **它只探到进程这一层。** 消费线程与 Brain 是不是还活着，这一帧答不了。
    """
    try:
        await connection.send(
            canonical_json(
                {
                    "v": 1,
                    "type": "heartbeat",
                    "payload": {"status": "alive", "connections": service.connections},
                }
            )
        )
    except Exception:
        _log.debug("心跳连接写不出去", exc_info=True)
    finally:
        await connection.close()


async def _serve_connection(service: SessionService, connection: ServerConnection) -> None:
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
                    scope = await asyncio.to_thread(service.open_session, command)
                except Exception as exc:
                    await connection.send(
                        canonical_json(close_frame(CloseReason.UNAUTHORIZED, str(exc)))
                    )
                    await connection.close()
                    return
                service.attach(scope, channel)
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
                if command.type == CommandType.SESSION_STATUS:
                    payload = await asyncio.to_thread(service.status, scope)
                    await connection.send(
                        canonical_json(
                            {
                                "v": 1,
                                "type": "event",
                                "session_id": scope.session_id,
                                "event_type": CommandType.SESSION_STATUS,
                                "payload": payload,
                            }
                        )
                    )
                    continue
                await asyncio.to_thread(service.submit, command, scope)
                await connection.send(canonical_json(ack(command.id, session_id=scope.session_id)))
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
            with contextlib.suppress(Exception):
                service.detach(scope, channel)


class WebSocketChannel(ClientChannel):
    def __init__(self, connection: ServerConnection) -> None:
        self.connection = connection

    async def send_text(self, text: str) -> None:
        await self.connection.send(text)

    async def close(self, reason: str) -> None:
        await self.connection.send(canonical_json(close_frame(reason)))
        await self.connection.close()


async def _fail(connection: ServerConnection, command_id: str | None, exc: Exception) -> None:
    """回一帧 error 就断。**帧不合法时不能只回错误继续收** —— 两端的认知已经分叉了。"""
    code = getattr(exc, "code", "internal_error")
    await connection.send(canonical_json(error_frame(command_id, code, str(exc))))
    if command_id is None:
        await connection.send(canonical_json(close_frame(CloseReason.PROTOCOL_ERROR, str(exc))))
        await connection.close()


async def serve(
    service: SessionService,
    *,
    host: str,
    port: int,
    max_connections: int,
    service_handler: Callable[[ServerConnection], Awaitable[None]] | None = None,
) -> None:
    """Serve chat and scoped service requests on the same listener."""
    active = {"chat": 0, "service": 0}

    async def handler(connection: ServerConnection) -> None:
        # 探测连接不占配额，也不参与顶替：它随时可能探，而业务连接不该被它挤掉。
        request = connection.request
        if request is not None and request.path == _HEARTBEAT_PATH:
            await _serve_heartbeat(connection, service)
            return
        kind = "service" if request is not None and request.path == "/service" else "chat"
        if active[kind] >= max_connections:
            # 直接断，不排队：排在后面等着的是一个已经知道自己连不上的客户端。
            await connection.send(
                canonical_json(close_frame(CloseReason.SLOW_CONSUMER, "连接数已满"))
            )
            await connection.close()
            return
        active[kind] += 1
        try:
            if request is not None and request.path == "/service" and service_handler is not None:
                await service_handler(connection)
            else:
                await _serve_connection(service, connection)
        finally:
            active[kind] -= 1

    async with _ws_serve(handler, host, port, max_size=_MAX_FRAME_BYTES, max_queue=16):
        _log.info("Oath 监听 ws://%s:%s（最多 %d 条连接）", host, port, max_connections)
        await asyncio.Future()


__all__ = ["serve"]
