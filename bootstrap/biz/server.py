"""biz 的 HTTP server：session 的开与关，加上观测查询与 CLI 代理。

整个 biz 一个 server、跑在一个端口上，每条能力是其中一个 path。**不为某个能力单开
一个 server** —— 加能力就是加一条路由。三组：`create_session` / `list_sessions`
管 scope 的生命周期，`/list_traces` 与 `/query_trace` 是只读查询，`/cli/<命令名>`
是业务 CLI 的代理（路由由 `biz/cli/cli_server.py` 给出）。

与 WebSocket 分开，两个理由：查询不是 command —— 它不产生 ACK、不改任何状态，
塞进 command/event 协议里是拧的；WS 上的 scope 靠 `session.open` 绑在连接上，
HTTP 每个请求自己带 credential、现验现用，没有那条隐式依赖。

**两套凭证，作用范围不同。** session 生命周期那两条认服务级凭证（配置里预置），
其余四条认 session 级凭证（manager 签发，只认那一个 scope）。

endpoint 写成同步的：Starlette 把**同步** endpoint 丢进线程池，文件 I/O 与拉进程
不会占住事件循环。写成 `async def` 就会在事件循环上直接跑那段阻塞代码，WS 的心跳
与事件投递会跟着一起卡住。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Final

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from biz.cli import cli_routes
from biz.service import BizService
from biz.session_scope import BEARER, error_response, scope_of
from biz.session_scope import BadQuery as _BadQuery
from biz.session_scope import Unauthorized as _Unauthorized
from bootstrap.manager import SessionLimitReached, SessionManager
from core.protocol.datatype.config import OathConfig
from core.protocol.error import WireProtocolError
from core.protocol.interface.agent_access import AgentUnavailable
from core.utils.encoding import JsonObject

_log = logging.getLogger("oath.http")

#: 列表一页给多少条。上限挡的是「一次把整个目录读进来」——不静默截断，超了直接回 400，
#: 否则客户端以为拿到的是全部。`offset` 不套这个上限：要翻到第 400 条不该拿不到。
_DEFAULT_LIMIT: Final = 50
_MAX_LIMIT: Final = 200

#: 关停时等 uvicorn 自己收尾的上限。它发 lifespan shutdown、等在途请求；卡住就到此为止。
_SHUTDOWN_TIMEOUT: Final = 5.0


def _count(request: Request, name: str, default: int, ceiling: int | None) -> int:
    """读一个非负整数的查询参数。不给就用默认值；`ceiling` 是上限，没有就是不限。

    超上限当不合法而不是截断：截断会让客户端以为拿到的那一截就是全部。
    """
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise _BadQuery(f"{name} 不是整数") from None
    if value < 0:
        raise _BadQuery(f"{name} 不能是负数")
    if ceiling is not None and value > ceiling:
        raise _BadQuery(f"{name} 上限是 {ceiling}")
    return value


def _list_traces(service: BizService, request: Request) -> JsonObject:
    """`GET /list_traces?limit=&offset=` —— 列一页这个 scope 的 trace。"""
    # 先认身份再读参数：参数不合法不该在没通过认证之前被试探出来。
    scope = scope_of(service, request)
    return service.list_traces(
        scope,
        limit=_count(request, "limit", _DEFAULT_LIMIT, _MAX_LIMIT),
        offset=_count(request, "offset", 0, None),
    )


def _query_trace(service: BizService, request: Request) -> JsonObject:
    """`GET /query_trace?trace_id=` —— 取一条 trace 的全部 span。"""
    scope = scope_of(service, request)
    # trace_id 不做格式校验：读不到就是空列表，与「无权查看」不可区分。
    trace_id = request.query_params.get("trace_id", "")
    if not trace_id:
        raise _BadQuery("缺 trace_id")
    return service.query_trace(scope, trace_id)


def _service_token(config: OathConfig, request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[len(BEARER) :].strip() if header.startswith(BEARER) else ""


def _endpoint(
    service: BizService, handler: Callable[[BizService, Request], JsonObject]
) -> Callable[[Request], Response]:
    def endpoint(request: Request) -> Response:
        try:
            return JSONResponse(handler(service, request))
        except (_Unauthorized, _BadQuery) as exc:
            return error_response(exc)
        except AgentUnavailable:
            return JSONResponse({"error": "agent_unavailable"}, status_code=503)
        except Exception:
            # 认证失败不该进错误日志当故障查，所以两种分开。
            _log.exception("HTTP %s 处理失败", request.url.path)
            return JSONResponse({"error": "internal_error"}, status_code=500)

    return endpoint


def build_app(service: BizService, manager: SessionManager, config: OathConfig) -> Starlette:
    """biz 的路由表。

    path 就是动作名，参数走 query string —— 不套 RESTful 的资源命名：`/traces` 既不是
    一个集合也不是一件东西，客户端要的是「列」「查」两个动作。

    `/heartbeat` 不认身份：存活探测走本机，它问的是「这个进程的 uvicorn 还处理请求吗」，
    不是「谁在问」。
    """

    async def heartbeat(request: Request) -> Response:
        return JSONResponse({"status": "alive"})

    async def create_session(request: Request) -> Response:
        try:
            if not config.service_authorised(_service_token(config, request)):
                # 正文不回显凭证：它多半是错的或过期的，抄进日志等于再泄一次。
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "bad_payload", "message": "请求体不是 JSON"}, 400)
        user_id = body.get("user_id") if isinstance(body, dict) else None
        if not isinstance(user_id, str):
            return JSONResponse({"error": "bad_payload", "message": "缺 user_id"}, 400)
        try:
            return JSONResponse(manager.create_session(user_id))
        except SessionLimitReached as exc:
            # 满了就是满了。排队意味着调用方要处理「等多久」，而它拿到返回时那个位置
            # 未必还空着。
            return JSONResponse({"error": "session_limit", "message": str(exc)}, 503)
        except WireProtocolError as exc:
            return JSONResponse({"error": exc.code, "message": str(exc)}, 400)
        except Exception:
            _log.exception("拉起 %s 的 session 失败", user_id)
            return JSONResponse({"error": "internal_error"}, status_code=500)

    async def list_sessions(request: Request) -> Response:
        if not config.service_authorised(_service_token(config, request)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(
            {
                "sessions": [
                    # `connections` 为 -1 表示探不到 —— 那与「0 条连接」是两件事。
                    {"user_id": user, "session_id": sid, "connections": count}
                    for user, sid, count in manager.sessions()
                ]
            }
        )

    return Starlette(
        routes=[
            Route("/heartbeat", heartbeat, methods=["GET"]),
            Route("/create_session", create_session, methods=["POST"]),
            Route("/list_sessions", list_sessions, methods=["GET"]),
            Route("/list_traces", _endpoint(service, _list_traces), methods=["GET"]),
            Route("/query_trace", _endpoint(service, _query_trace), methods=["GET"]),
            *cli_routes(service),
        ]
    )


async def serve(
    service: BizService, manager: SessionManager, config: OathConfig, *, host: str, port: int
) -> None:
    """起 biz 的 HTTP server，一直跑到被取消。"""
    settings = uvicorn.Config(
        build_app(service, manager, config),
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    _log.info("biz server 监听 http://%s:%s", host, port)
    server = uvicorn.Server(settings)
    try:
        await server.serve()
    except SystemExit as exc:
        # uvicorn 绑不上端口时抛 SystemExit。那是 BaseException，asyncio 不会把它当成
        # task 的异常上报，而是直接掀掉事件循环 —— 外面根本等不到「这个 server 挂了」。
        # 换成 OSError，让它按普通 task 失败走。
        raise OSError(f"{host}:{port} 起不来") from exc
    except asyncio.CancelledError:
        # uvicorn 有自己的收尾流程（发 lifespan shutdown、等在途请求）。直接 cancel 会把
        # 它内部的 lifespan task 打成一条 ERROR，看着像故障 —— 让它自己走完。
        server.should_exit = True
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(server.shutdown(), timeout=_SHUTDOWN_TIMEOUT)


__all__ = ["build_app", "serve"]
