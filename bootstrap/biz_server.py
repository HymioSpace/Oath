"""biz 的 HTTP server：对外暴露观测查询与 CLI 代理。

整个 biz 一个 server、跑在一个端口上，每条能力是其中一个 path。**不为某个能力单开
一个 server** —— 加能力就是加一条路由。当前两组：`/list_traces` 与 `/query_trace`
是只读查询，`/cli/<命令名>` 是业务 CLI 的代理（路由由 `biz/cli/cli_server.py` 给出）。

与 WebSocket 分开，两个理由：查询不是 command —— 它不产生 ACK、不改任何状态，
塞进 command/event 协议里是拧的；WS 上的 scope 靠 `session.open` 绑在连接上，
HTTP 每个请求自己带 credential、现验现用，没有那条隐式依赖。

身份仍然走同一个 `SessionAuthenticator`，与 WebSocket 一套。

endpoint 写成同步的：Starlette 把**同步** endpoint 丢进线程池，文件 I/O 不会占住
事件循环。写成 `async def` 就会在事件循环上直接跑那段阻塞查询，WS 的心跳与事件投递
会跟着一起卡住。
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
from biz.session_scope import BadQuery as _BadQuery
from biz.session_scope import Unauthorized as _Unauthorized
from biz.session_scope import error_response, scope_of
from biz.trace import list_traces, query_trace
from bootstrap.runtime import Runtime
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


def _list_traces(runtime: Runtime, request: Request) -> JsonObject:
    """`GET /list_traces?limit=&offset=` —— 列一页这个 scope 的 trace。"""
    # 先认身份再读参数：参数不合法不该在没通过认证之前被试探出来。
    scope = scope_of(runtime.authenticator, request)
    return list_traces(
        runtime.traces,
        scope,
        limit=_count(request, "limit", _DEFAULT_LIMIT, _MAX_LIMIT),
        offset=_count(request, "offset", 0, None),
    )


def _query_trace(runtime: Runtime, request: Request) -> JsonObject:
    """`GET /query_trace?trace_id=` —— 取一条 trace 的全部 span。"""
    scope = scope_of(runtime.authenticator, request)
    # trace_id 不做格式校验：读不到就是空列表，与「无权查看」不可区分。
    trace_id = request.query_params.get("trace_id", "")
    if not trace_id:
        raise _BadQuery("缺 trace_id")
    return query_trace(runtime.traces, scope, trace_id)


def _endpoint(
    runtime: Runtime, handler: Callable[[Runtime, Request], JsonObject]
) -> Callable[[Request], Response]:
    def endpoint(request: Request) -> Response:
        try:
            return JSONResponse(handler(runtime, request))
        except (_Unauthorized, _BadQuery) as exc:
            return error_response(exc)
        except Exception:
            # 认证失败不该进错误日志当故障查，所以两种分开。
            _log.exception("HTTP %s 处理失败", request.url.path)
            return JSONResponse({"error": "internal_error"}, status_code=500)

    return endpoint


def build_app(runtime: Runtime) -> Starlette:
    """biz 的路由表。

    path 就是动作名，参数走 query string —— 不套 RESTful 的资源命名：`/traces` 既不是
    一个集合也不是一件东西，客户端要的是「列」「查」两个动作。
    """
    return Starlette(
        routes=[
            Route("/list_traces", _endpoint(runtime, _list_traces), methods=["GET"]),
            Route("/query_trace", _endpoint(runtime, _query_trace), methods=["GET"]),
            *cli_routes(runtime),
        ]
    )


async def serve(runtime: Runtime, *, host: str, port: int) -> None:
    """起 biz 的 HTTP server，一直跑到被取消。"""
    config = uvicorn.Config(
        build_app(runtime), host=host, port=port, log_level="warning", access_log=False
    )
    _log.info("biz server 监听 http://%s:%s", host, port)
    server = uvicorn.Server(config)
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
