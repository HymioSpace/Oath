"""Biz HTTP 路由；trace 与 CLI 不经过 WS。"""

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from biz.cli import cli_routes
from biz.user_identity import BadQuery, Unauthorized, error_response, user_of


def build_app(service, cli_service):
    def traces(request):
        try:
            user = user_of(service, request)
            limit = int(request.query_params.get("limit", 50))
            offset = int(request.query_params.get("offset", 0))
            if not 0 <= limit <= 200 or offset < 0:
                raise BadQuery("Invalid pagination")
            return JSONResponse(service.list_traces(user, limit=limit, offset=offset))
        except (Unauthorized, BadQuery) as exc:
            return error_response(exc)
        except ValueError:
            return JSONResponse({"error": "bad_query"}, 400)

    def trace(request):
        try:
            user = user_of(service, request)
            return JSONResponse(service.query_trace(user, request.query_params.get("trace_id", "")))
        except (Unauthorized, BadQuery) as exc:
            return error_response(exc)

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

    def heartbeat(request):
        return JSONResponse({"status": "alive"})

    return Starlette(
        routes=[
            Route("/heartbeat", heartbeat),
            Route("/conversation_history", history),
            Route("/list_traces", traces),
            Route("/query_trace", trace),
            *cli_routes(cli_service),
        ]
    )
