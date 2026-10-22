"""有界 HTTP 传输与身份检查；具体行为由各服务的闭合操作集合提供。"""

import asyncio
import hmac
import json
import logging
import time
from urllib.parse import urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.observability import (
    RPC_DURATION,
    RPC_INFLIGHT,
    RPC_PAYLOAD_BYTES,
    RPC_REQUESTS,
    metrics_response,
)
from core.protocol.interface.storage import LeaseLost, StorageConflict

_log = logging.getLogger(__name__)


def rpc_app(service, token, operations, service_name="rpc"):
    capacity = asyncio.Semaphore(4 if service_name == "storage" else 8)

    def invoke(operation, body):
        params = json.loads(body)
        if not isinstance(params, dict):
            raise ValueError("Expected object")
        return JSONResponse({"result": service.call(operation, **params)})

    async def execute(request: Request):
        try:
            await asyncio.wait_for(capacity.acquire(), timeout=2)
        except TimeoutError:
            RPC_REQUESTS.labels(service_name, request.path_params["operation"], "503").inc()
            return JSONResponse({"error": "service_busy"}, 503)
        try:
            return await execute_admitted(request)
        finally:
            capacity.release()

    async def execute_admitted(request: Request):
        started = time.monotonic()
        operation = request.path_params["operation"]
        status = 500
        if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}"):
            status = 401
            return JSONResponse({"error": "unauthorized"}, 401)
        if operation not in operations:
            status = 404
            return JSONResponse({"error": "unknown_operation"}, 404)
        RPC_INFLIGHT.labels(service_name).inc()
        try:
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 32 * 1024 * 1024:
                    status = 413
                    return JSONResponse({"error": "request_too_large"}, 413)
                body.extend(chunk)
            RPC_PAYLOAD_BYTES.labels(service_name, operation).observe(len(body))
            # Parsing and serializing accumulated state must not block heartbeats
            # and lease renewals on the ASGI event loop.
            response = await run_in_threadpool(invoke, operation, body)
            status = 200
            return response
        except StorageConflict as exc:
            status = 409
            return JSONResponse({"error": str(exc)}, 409)
        except LeaseLost as exc:
            status = 412
            return JSONResponse({"error": str(exc)}, 412)
        except (ValueError, KeyError, TypeError) as exc:
            status = 400
            return JSONResponse({"error": str(exc)}, 400)
        except Exception:
            status = 503
            _log.exception("RPC %s failed", operation)
            return JSONResponse({"error": "service_unavailable"}, 503)
        finally:
            RPC_INFLIGHT.labels(service_name).dec()
            RPC_REQUESTS.labels(service_name, operation, str(status)).inc()
            RPC_DURATION.labels(service_name, operation, str(status)).observe(
                time.monotonic() - started
            )

    async def heartbeat(request):
        return JSONResponse({"status": "alive"})

    async def metrics(request):
        if hasattr(service, "refresh_metrics"):
            await run_in_threadpool(service.refresh_metrics)
        body, content_type = await run_in_threadpool(metrics_response)
        return Response(body, media_type=content_type)

    return Starlette(
        routes=[
            Route("/heartbeat", heartbeat),
            Route("/metrics", metrics),
            Route("/v1/{operation}", execute, methods=["POST"]),
        ]
    )


def serve_rpc(service, token, operations, url, *, bind_host=None, service_name="rpc"):
    address = urlsplit(url)
    uvicorn.run(
        rpc_app(service, token, operations, service_name),
        host=bind_host or address.hostname,
        port=address.port,
        access_log=False,
    )
