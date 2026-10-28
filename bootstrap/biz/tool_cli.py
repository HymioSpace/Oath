"""CLI transport over the canonical ToolSystem; no second execution registry."""

import hashlib
from pathlib import Path

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from bootstrap.common.authentication import cli_claims
from core.protocol.error import PermissionDeniedError, ToolError

CLIENT = Path(__file__).resolve().parents[2] / "scripts/oath-cli"


def routes(config, tools):
    async def endpoint(request):
        try:
            if request.url.path == "/cli/client":
                return Response(CLIENT.read_bytes(), media_type="application/octet-stream")
            if request.url.path == "/cli/install":
                return Response(
                    CLIENT.with_name("install-oath-cli").read_bytes(),
                    media_type="application/octet-stream",
                )
            if request.url.path == "/cli/manifest":
                return JSONResponse({"sha256": hashlib.sha256(CLIENT.read_bytes()).hexdigest()})
            credential = request.headers.get("authorization", "").removeprefix("Bearer ")
            cli_claims(config, credential)
            common = {"credential": credential}
            if request.method == "GET":
                return JSONResponse(
                    await asyncio.to_thread(tools.call, "tools.cli_catalog", **common)
                )
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 65536:
                    return JSONResponse({"error": "request_too_large"}, 413)
            import json

            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Expected object")
            if request.url.path == "/cli/status":
                return JSONResponse(
                    await asyncio.to_thread(
                        tools.call,
                        "tools.cli_status",
                        **common,
                        invocation_id=data["invocation_id"],
                    )
                )
            if request.url.path == "/cli/detach":
                return JSONResponse(
                    await asyncio.to_thread(
                        tools.call,
                        "tools.cli_detach",
                        **common,
                        invocation_id=data["invocation_id"],
                    )
                )
            return JSONResponse(
                await asyncio.to_thread(
                    tools.call,
                    "tools.cli_submit",
                    **common,
                    **{
                        key: data[key]
                        for key in ("name", "arguments", "request_id", "async")
                        if key in data
                    },
                )
            )
        except PermissionDeniedError:
            return JSONResponse({"error": "unauthorized"}, 401)
        except ToolError:
            return JSONResponse({"error": "invalid_cli_request"}, 400)
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "invalid_cli_request"}, 400)

    # Synchronous RPC calls run outside the ASGI loop.
    import asyncio

    async def admitted(request):
        # Each CLI request is short: submit or status, never wait for a tool in Biz.
        return await endpoint(request)

    return [
        Route(p, admitted, methods=methods)
        for p, methods in (
            ("/cli/client", ["GET"]),
            ("/cli/install", ["GET"]),
            ("/cli/manifest", ["GET"]),
            ("/cli/tools", ["GET"]),
            ("/cli/invoke", ["POST"]),
            ("/cli/status", ["POST"]),
            ("/cli/detach", ["POST"]),
        )
    ]
