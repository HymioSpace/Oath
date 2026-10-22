"""WS 接入与传输；持续执行属于 Worker，重放属于 Outbox。"""

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import replace

from websockets.asyncio.server import serve

from bootstrap.clients.service_client import ServiceClient
from bootstrap.common.authentication import UserAuthenticator, service_token
from core.observability import (
    GATEWAY_COMMANDS,
    GATEWAY_CONNECTIONS,
    GATEWAY_DURATION,
    metrics_response,
)
from core.observability.spans import append_span
from core.outbound.delivery import Delivery
from core.protocol.datatype.inbound import of_command
from core.protocol.datatype.trace import new_trace_id
from core.protocol.datatype.user import User
from core.protocol.datatype.wire import Command, CommandType, ack, error_frame, pong
from core.trace.attributes import OATH_CHANNEL

_log = logging.getLogger(__name__)


def validate_model(config, command: Command) -> None:
    """模型 ID 只能取自运维给的清单。

    客户端可以挑模型，但挑不了端点和凭据 —— 超出清单的在这里挡掉，
    别让它一路走到 provider 才炸。
    """
    if command.type != CommandType.USER_MESSAGE or "model" not in command.payload:
        return
    model = command.payload["model"]
    if not isinstance(model, str) or model not in {
        config.agent.provider.model,
        *config.agent.selectable_models,
        *config.agent.model_providers,
    }:
        raise ValueError("Model is not available")


async def run_async(config):
    token = service_token(config)
    storage = ServiceClient(config.services.storage_url, token, config.services.request_timeout)
    queue = ServiceClient(config.services.queue_url, token, config.services.request_timeout)
    auth = UserAuthenticator(config)
    active = 0

    async def handler(connection):
        nonlocal active
        if active >= config.services.max_connections:
            await connection.close(code=1013, reason="capacity")
            return
        active += 1
        GATEWAY_CONNECTIONS.inc()
        send_lock = asyncio.Lock()
        sender = None
        receiver = None
        monitor = None
        channel = "unknown"

        async def send(frame):
            async with send_lock:
                await asyncio.wait_for(
                    connection.send(json.dumps(frame, ensure_ascii=False)), timeout=10
                )

        try:
            command = Command.parse(json.loads(await asyncio.wait_for(connection.recv(), 10)))
            if command.type != CommandType.AGENT_ATTACH:
                raise ValueError("First command must be agent.attach")
            credential = command.payload.get("credential", "")
            supplied_user = command.payload.get("user_id")
            candidate = command.payload.get("channel_kind")
            if isinstance(candidate, str) and 0 < len(candidate) <= 32:
                channel = candidate
            identity = await asyncio.to_thread(auth.authenticate, credential, supplied_user)
            user = User(identity.user_id)

            async def verify():
                current = await asyncio.to_thread(auth.authenticate, credential, supplied_user)
                if current.user_id != user.user_id:
                    raise ValueError("Connection identity changed")
                return current

            async def watch_identity():
                current = identity
                while True:
                    delay = (
                        30
                        if current.expires_at is None
                        else min(30, max(0, current.expires_at - time.time()))
                    )
                    await asyncio.sleep(delay)
                    if current.expires_at is not None and time.time() >= current.expires_at:
                        raise ValueError("Credential expired")
                    current = await verify()

            delivery = Delivery(
                storage,
                user.user_id,
                command.payload.get("client_id"),
                send,
                config.services.poll_seconds,
            )
            attached = await delivery.attach(command.payload.get("history_limit"))
            await send(ack(command.id, user_id=user.user_id))

            async def receive():
                async for raw in connection:
                    await verify()
                    command = None
                    started = time.monotonic()
                    status = "error"
                    try:
                        command = Command.parse(json.loads(raw))
                        if command.type == CommandType.EVENT_ACK:
                            await delivery.confirm(command.payload.get("cursor"))
                        elif command.type == CommandType.PING:
                            await send(pong(command.id))
                        elif command.type == CommandType.AGENT_STATUS:
                            state = await asyncio.to_thread(
                                storage.call, "state.summary", user_id=user.user_id
                            )
                            await send(
                                {
                                    "v": 2,
                                    "type": "agent.status",
                                    "id": command.id,
                                    "payload": state,
                                }
                            )
                        else:
                            validate(command)
                            validate_model(config, command)
                            item = of_command(command)
                            trace_id = None
                            if item is not None and command.type == CommandType.USER_MESSAGE:
                                trace_id = new_trace_id()
                                item = replace(
                                    item,
                                    telemetry={
                                        "trace_id": trace_id,
                                        "channel": channel,
                                        "received_at_unix_ms": round(time.time() * 1000),
                                    },
                                )
                            queued_at = time.time_ns()
                            await asyncio.to_thread(
                                queue.call, "queue.add", user_id=user.user_id, item=item.to_dict()
                            )
                            await send(
                                ack(command.id, **({"trace_id": trace_id} if trace_id else {}))
                            )
                            if trace_id:
                                append_span(
                                    storage,
                                    user_id=user.user_id,
                                    trace_id=trace_id,
                                    name="gateway.accept",
                                    started_ns=queued_at,
                                    ended_ns=time.time_ns(),
                                    attributes={
                                        OATH_CHANNEL: channel,
                                        "oath.command.type": command.type,
                                    },
                                )
                        status = "ok"
                    except Exception as exc:
                        await send(
                            error_frame(command.id if command else None, "command_failed", str(exc))
                        )
                    finally:
                        type_ = command.type if command else "unknown"
                        GATEWAY_COMMANDS.labels(type_, channel, status).inc()
                        GATEWAY_DURATION.labels(type_, channel, status).observe(
                            time.monotonic() - started
                        )

            monitor = asyncio.create_task(watch_identity())
            sender = asyncio.create_task(delivery.run(attached))
            receiver = asyncio.create_task(receive())
            done, _ = await asyncio.wait(
                {sender, receiver, monitor}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
        except Exception as exc:
            _log.info("WS closed: %s", type(exc).__name__)
            with contextlib.suppress(Exception):
                await send(error_frame(None, "connection_closed", str(exc)))
        finally:
            tasks = [task for task in (sender, receiver, monitor) if task]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await connection.close()
            active -= 1
            GATEWAY_CONNECTIONS.dec()

    def health(connection, request):
        if request.path == "/heartbeat":
            return connection.respond(200, '{"alive":true}\n')
        if request.path == "/metrics":
            body, _ = metrics_response()
            return connection.respond(200, body.decode())
        return None

    try:
        async with serve(
            handler,
            config.services.gateway_host,
            config.services.gateway_port,
            process_request=health,
            max_size=1024 * 1024,
            max_queue=16,
        ):
            await asyncio.Future()
    finally:
        storage.close()
        queue.close()


def validate(command):
    if command.type == CommandType.USER_MESSAGE:
        if not isinstance(command.payload.get("content"), str) or not isinstance(
            command.payload.get("user_context"), dict
        ):
            raise ValueError("user.message requires content and user_context")
        if any(not key for key in command.payload["user_context"]):
            raise ValueError("user_context keys must be nonempty")
    elif command.type == CommandType.PERMISSION_DECIDE:
        if command.payload.get("status") not in {"approved", "denied"} or not isinstance(
            command.payload.get("invocation_id"), str
        ):
            raise ValueError("Invalid permission decision")
    elif command.type not in {CommandType.RUN_CANCEL, CommandType.STEP_RETRY}:
        raise ValueError("Unsupported command")


def run(config):
    asyncio.run(run_async(config))
