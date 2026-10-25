"""Biz HTTP 路由；trace 与 CLI 不经过 WS。"""

import asyncio
import hmac
import json
import time
from datetime import UTC, datetime

from starlette.applications import Starlette
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from biz.cli import cli_routes
from biz.execution_status import execution_status
from biz.user_identity import BadQuery, Unauthorized, error_response, user_of
from core.observability import TRACE_API_DURATION, metrics_response
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.user import User, is_valid_user_id


def build_app(service, cli_service, admin_token=""):
    def traces(request):
        started = time.monotonic()
        status = "ok"
        try:
            user = user_of(service, request)
            limit = int(request.query_params.get("limit", 50))
            offset = int(request.query_params.get("offset", 0))
            if not 0 <= limit <= 200 or offset < 0:
                raise BadQuery("Invalid pagination")
            return JSONResponse(service.list_traces(user, limit=limit, offset=offset))
        except (Unauthorized, BadQuery) as exc:
            status = "error"
            return error_response(exc)
        except ValueError:
            status = "error"
            return JSONResponse({"error": "bad_query"}, 400)
        finally:
            TRACE_API_DURATION.labels("list", status).observe(time.monotonic() - started)

    def trace(request):
        started = time.monotonic()
        status = "ok"
        try:
            user = user_of(service, request)
            return JSONResponse(
                service.query_trace(
                    user,
                    request.query_params.get("trace_id", ""),
                    summary=request.query_params.get("summary") == "1",
                )
            )
        except (Unauthorized, BadQuery) as exc:
            status = "error"
            return error_response(exc)
        finally:
            TRACE_API_DURATION.labels("query", status).observe(time.monotonic() - started)

    def admin_user(request):
        supplied = request.headers.get("authorization", "")
        if not admin_token or not hmac.compare_digest(supplied, f"Bearer {admin_token}"):
            raise Unauthorized()
        value = request.query_params.get("user_id", "")
        if not is_valid_user_id(value):
            raise Unauthorized()
        return User(value)

    def admin_traces(request):
        started = time.monotonic()
        status = "ok"
        try:
            user = admin_user(request)
            limit = int(request.query_params.get("limit", 30))
            offset = int(request.query_params.get("offset", 0))
            if not 0 <= limit <= 200 or offset < 0:
                raise BadQuery("Invalid pagination")
            return JSONResponse(service.list_traces(user, limit=limit, offset=offset))
        except (Unauthorized, BadQuery, ValueError):
            status = "error"
            return JSONResponse({"error": "unauthorized"}, 401)
        finally:
            TRACE_API_DURATION.labels("admin_list", status).observe(time.monotonic() - started)

    def admin_trace(request):
        started = time.monotonic()
        status = "ok"
        try:
            user = admin_user(request)
            return JSONResponse(
                service.query_trace(
                    user,
                    request.query_params.get("trace_id", ""),
                    summary=request.query_params.get("summary") == "1",
                )
            )
        except (Unauthorized, BadQuery, ValueError):
            status = "error"
            return JSONResponse({"error": "unauthorized"}, 401)
        finally:
            TRACE_API_DURATION.labels("admin_query", status).observe(time.monotonic() - started)

    def admin_compactions(request):
        supplied = request.headers.get("authorization", "")
        if not admin_token or not hmac.compare_digest(supplied, f"Bearer {admin_token}"):
            return JSONResponse({"error": "unauthorized"}, 401)
        try:
            hours = min(168, max(1, int(request.query_params.get("hours", 24))))
            since = datetime.fromtimestamp(time.time() - hours * 3600, UTC).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            )
            return JSONResponse({"events": service.compaction_events(since=since), "hours": hours})
        except (TypeError, ValueError):
            return JSONResponse({"error": "bad_query"}, 400)

    def history(request):
        try:
            user = user_of(service, request)
            before = request.query_params.get("before")
            return JSONResponse(
                service.conversation_history(
                    user,
                    before=int(before) if before is not None else None,
                    limit=int(request.query_params.get("limit", 10)),
                )
            )
        except (Unauthorized, BadQuery) as exc:
            return error_response(exc)
        except ValueError:
            return JSONResponse({"error": "bad_query"}, 400)

    async def agent_operation(request):
        try:
            user = await asyncio.to_thread(user_of, service, request)
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 65536:
                    raise ValueError("Request too large")
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                raise ValueError("Expected object")
            if request.url.path == "/executions/get":
                invocation_id = data.get("invocation_id")
                if not isinstance(invocation_id, str) or not 1 <= len(invocation_id) <= 256:
                    raise ValueError("Invalid invocation")
                snapshot = await asyncio.to_thread(service.agent_operation, user, "state.read")
                return JSONResponse(execution_status(snapshot["state"], invocation_id))
            if request.url.path == "/events":
                source = data["type"]
                if (
                    not isinstance(source, str)
                    or not source.startswith("app.")
                    or len(source) > 128
                ):
                    raise ValueError("Application event types must start with app.")
                if (
                    not isinstance(data.get("event_id"), str)
                    or not 1 <= len(data["event_id"]) <= 256
                ):
                    raise ValueError("event_id required")
                if not isinstance(data.get("payload", {}), dict):
                    raise ValueError("Invalid payload")
                queue = data.get("queue", "system")
                if queue not in {"user", "system"}:
                    raise ValueError("Application event queue must be user or system")
                defer_wake = data.get("defer_wake", False)
                if not isinstance(defer_wake, bool):
                    raise ValueError("defer_wake must be boolean")
                if defer_wake and queue != "user":
                    raise ValueError("defer_wake is only valid for the user queue")
                item = Inbound(
                    queue,
                    source,
                    data.get("payload", {}),
                    data["event_id"],
                    telemetry={"defer_wake": True} if defer_wake else {},
                )
                result = await asyncio.to_thread(
                    service.agent_operation, user, "queue.add", item=item.to_dict()
                )
                return JSONResponse({"accepted": True, "id": result})
            if request.url.path == "/messages/react":
                result = await asyncio.to_thread(
                    service.agent_operation,
                    user,
                    "message.react",
                    actor=data.get("actor"),
                    target=data.get("target"),
                    emoji=data.get("emoji"),
                    operation_id=data.get("operation_id"),
                )
                return JSONResponse(result)
            action = request.path_params["action"]
            if action not in {"spawn", "list", "get", "cancel", "continue"}:
                raise ValueError("Unknown action")
            if "user_id" in data or "fence" in data:
                raise ValueError("Identity is derived from credentials")
            result = await asyncio.to_thread(
                service.agent_operation, user, "subagent." + action, **data
            )
            return JSONResponse(result)
        except (Unauthorized, BadQuery) as exc:
            return error_response(exc)
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "bad_request"}, 400)

    def heartbeat(request):
        return JSONResponse({"status": "alive"})

    def metrics(request):
        body, content_type = metrics_response()
        return Response(body, media_type=content_type)

    app = Starlette(
        routes=[
            Route("/heartbeat", heartbeat),
            Route("/metrics", metrics),
            Route("/admin/traces", admin_traces),
            Route("/admin/trace", admin_trace),
            Route("/admin/compactions", admin_compactions),
            Route("/events", agent_operation, methods=["POST"]),
            Route("/messages/react", agent_operation, methods=["POST"]),
            Route("/executions/get", agent_operation, methods=["POST"]),
            Route("/subagents/{action}", agent_operation, methods=["POST"]),
            Route("/conversation_history", history),
            Route("/list_traces", traces),
            Route("/query_trace", trace),
            *cli_routes(cli_service),
        ]
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    return app
