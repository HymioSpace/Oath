"""有界 HTTP 传输与身份检查；具体行为由各服务的闭合操作集合提供。"""

import hmac
import logging
from urllib.parse import urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from core.protocol.interface.storage import LeaseLost, StorageConflict

_log = logging.getLogger(__name__)


def rpc_app(service, token, operations):
    async def execute(request: Request):
        if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}"):
            return JSONResponse({"error": "unauthorized"}, 401)
        operation = request.path_params["operation"]
        if operation not in operations:
            return JSONResponse({"error": "unknown_operation"}, 404)
        try:
            body = await request.body()
            if len(body) > 32 * 1024 * 1024:
                return JSONResponse({"error": "request_too_large"}, 413)
            params = await request.json()
            if not isinstance(params, dict):
                raise ValueError("Expected object")
            result = await run_in_threadpool(service.call, operation, **params)
            return JSONResponse({"result": result})
        except StorageConflict as exc:
            return JSONResponse({"error": str(exc)}, 409)
        except LeaseLost as exc:
            return JSONResponse({"error": str(exc)}, 412)
        except (ValueError, KeyError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, 400)
        except Exception:
            _log.exception("RPC %s failed", operation)
            return JSONResponse({"error": "service_unavailable"}, 503)

    async def heartbeat(request):
        return JSONResponse({"status": "alive"})

    return Starlette(
        routes=[Route("/heartbeat", heartbeat), Route("/v1/{operation}", execute, methods=["POST"])]
    )


def serve_rpc(service, token, operations, url, *, bind_host=None):
    address = urlsplit(url)
    uvicorn.run(
        rpc_app(service, token, operations),
        host=bind_host or address.hostname,
        port=address.port,
        access_log=False,
    )
