"""WS 接入与传输；持续执行属于 Worker，重放属于 Outbox。"""

import asyncio
import contextlib
import json
import logging

from websockets.asyncio.server import serve

from bootstrap.clients.service_client import ServiceClient
from bootstrap.common.authentication import UserAuthenticator, service_token
from core.outbound.delivery import Delivery
from core.protocol.datatype.inbound import of_command
from core.protocol.datatype.wire import Command, CommandType, ack, error_frame, pong

_log = logging.getLogger(__name__)


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
        send_lock = asyncio.Lock()
        sender = None
        receiver = None

        async def send(frame):
            async with send_lock:
                await asyncio.wait_for(
                    connection.send(json.dumps(frame, ensure_ascii=False)), timeout=10
                )

        try:
            command = Command.parse(json.loads(await asyncio.wait_for(connection.recv(), 10)))
            if command.type != CommandType.AGENT_ATTACH:
                raise ValueError("First command must be agent.attach")
            user = auth.authenticate(command.payload.get("credential", ""))
            delivery = Delivery(
                storage,
                user.user_id,
                command.payload.get("client_id"),
                send,
                config.services.poll_seconds,
            )
            attached = await delivery.attach()
            await send(ack(command.id, user_id=user.user_id))

            async def receive():
                async for raw in connection:
                    command = None
                    try:
                        command = Command.parse(json.loads(raw))
                        if command.type == CommandType.EVENT_ACK:
                            await delivery.confirm(command.payload.get("cursor"))
                        elif command.type == CommandType.PING:
                            await send(pong(command.id))
                        elif command.type == CommandType.AGENT_STATUS:
                            state = await asyncio.to_thread(
                                storage.call, "state.read", user_id=user.user_id
                            )
                            await send(
                                {
                                    "v": 2,
                                    "type": "agent.status",
                                    "id": command.id,
                                    "payload": state["state"],
                                }
                            )
                        else:
                            validate(command)
                            item = of_command(command)
                            await asyncio.to_thread(
                                queue.call, "queue.add", user_id=user.user_id, item=item.to_dict()
                            )
                            await send(ack(command.id))
                    except Exception as exc:
                        await send(
                            error_frame(command.id if command else None, "command_failed", str(exc))
                        )

            sender = asyncio.create_task(delivery.run(attached))
            receiver = asyncio.create_task(receive())
            done, _ = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except Exception as exc:
            _log.info("WS closed: %s", type(exc).__name__)
            with contextlib.suppress(Exception):
                await send(error_frame(None, "connection_closed", str(exc)))
        finally:
            tasks = [task for task in (sender, receiver) if task]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await connection.close()
            active -= 1

    def health(connection, request):
        if request.path == "/heartbeat":
            return connection.respond(200, '{"alive":true}\n')
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
