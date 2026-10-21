"""从 HTTP 请求里认出这个请求属于哪个 user。

biz 的几条 path 共用同一份 外部身份验证，也共用同一种回错方式。放在 biz
而不是 server 里：认证器由启动层注入，业务路由只消费已验证身份。
"""

from __future__ import annotations

from typing import Final

from starlette.requests import Request
from starlette.responses import JSONResponse

from biz.service import BizService
from core.protocol.datatype.user import User
from core.protocol.error import PermissionDeniedError

#: credential 从这个头里取。查表换身份，与 WebSocket 的 `agent.attach` 使用同一认证器。
BEARER: Final = "Bearer "


class Unauthorized(Exception):
    """credential 不认。由 `error_response` 回 401。"""


class BadQuery(Exception):
    """参数不合法。由 `error_response` 回 400。"""


def user_of(service: BizService, request: Request) -> User:
    """认出 user。

    认不出就抛 `Unauthorized` 而不是回一个空 user：认不出身份就只能当没登录，
    不能退化成某个默认账号。
    """
    header = request.headers.get("authorization", "")
    token = header[len(BEARER) :].strip() if header.startswith(BEARER) else ""
    try:
        identity = service.authenticator.authenticate(token, request.headers.get("x-oath-user-id"))
    except PermissionDeniedError:
        # 错误正文不带 credential 本身：它多半是错的或过期的，抄进日志等于再泄一次。
        raise Unauthorized() from None
    return User(identity.user_id, identity.agent_id)


def error_response(exc: Exception) -> JSONResponse:
    """认证与参数这两类错的回法。业务自己抛别的异常不算在这里，由调用方当故障查。"""
    if isinstance(exc, Unauthorized):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"error": "bad_query", "message": str(exc)}, status_code=400)


__all__ = ["BEARER", "BadQuery", "Unauthorized", "error_response", "user_of"]
