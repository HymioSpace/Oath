"""CLI server：把业务命令暴露成 HTTP path，让 `oath-cli` 在执行环境里代打。

一条命令一个 path：`POST /cli/<命令名>`，参数是 JSON body；`GET /cli_commands` 是命令目录。
**执行用 POST 而不是 GET** —— 命令是动作；将来代理写操作时，GET 的缓存、预取与重试
语义都不对。

**鉴权只有一处**：请求头里的 credential 换成 scope，之后 `user_context` 整份交给命令 ——
框架不解释它、不拿它判权限，要不要用、怎么判是业务自己的事。

命令本身在扩展目录里声明（`COMMANDS`，见 `spec/feature-extensions.md`），启动时扫出来
由 `Runtime` 持有。handler 的签名统一：参数按 `parameters` 声明的名字接，另有两个
keyword-only 参数由框架给：

- `scope`：这次调用属于哪个会话；
- `user_context`：客户端随消息带来的 kv，已裁掉超过 24 小时没碰过的项。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from biz.session_scope import BadQuery, Unauthorized, error_response, scope_of
from bootstrap.runtime import Runtime
from core.context.user_context import current, table_of
from core.protocol.datatype import CliCommand
from core.protocol.datatype.scope import Scope
from core.utils.encoding import JsonObject, JsonValue

_log = logging.getLogger("oath.cli")


def cli_routes(runtime: Runtime) -> list[Route]:
    """CLI 的两条路由。

    与观测查询同属 biz server、同一个端口、同一套 credential —— 加能力就是加一条
    path，不为 CLI 单开一个进程。
    """
    return [
        Route("/cli_commands", _commands_endpoint(runtime), methods=["GET"]),
        Route("/cli/{name}", _command_endpoint(runtime), methods=["POST"]),
    ]


def _commands_endpoint(runtime: Runtime) -> Response:
    """`GET /cli_commands` —— 命令名、说明与参数。

    `oath-cli` 不带参数时打这条来列用法，所以参数声明要完整：位置参数的顺序就在
    `parameters` 里。不认身份 —— 这张表没有调用方的东西。
    """
    return JSONResponse(
        {
            "commands": [
                {"name": c.name, "description": c.description, "parameters": c.parameters}
                for c in runtime.commands
            ]
        }
    )


def _command_endpoint(runtime: Runtime) -> Callable[[Request], Any]:
    """`POST /cli/<命令名>` —— 校验 body、认身份、跑命令。

    endpoint 是 async 的，因为要读 body；命令本身同步，丢进线程池跑 —— 直接在事件循环
    上跑它，WS 的心跳与事件投递会跟着一起卡住。
    """

    async def endpoint(request: Request) -> Response:
        name = request.path_params["name"]
        command = _command(runtime, name)
        if command is None:
            # 命令名不认不该算认证失败：先看有没有这个能力，再谈是谁在问。
            return JSONResponse({"error": "unknown_command", "name": name}, status_code=404)
        try:
            # 先认身份再读 body：参数不合法不该在没通过认证之前被试探出来。
            scope = scope_of(runtime.authenticator, request)
            arguments = await _arguments(command, request)
        except (Unauthorized, BadQuery) as exc:
            return error_response(exc)

        try:
            return JSONResponse(await run_in_threadpool(_run, runtime, command, scope, arguments))
        except Exception:
            # 命令自己的失败是业务问题，但它没自己回错就是这条 path 的故障，要能查。
            _log.exception("CLI %s 执行失败", name)
            return JSONResponse({"error": "command_failed"}, status_code=500)

    return endpoint


def _command(runtime: Runtime, name: str) -> CliCommand | None:
    return next((c for c in runtime.commands if c.name == name), None)


def _run(runtime: Runtime, command: CliCommand, scope: Scope, arguments: JsonObject) -> JsonValue:
    """跑命令，顺带把该 session 的 user_context 交进去。

    整条路径（读状态、跑命令）都在线程池里，不占事件循环。
    """
    return command.handler(
        **arguments,
        scope=scope,
        user_context=current(table_of(runtime.ctx.store.read(scope)), now=time.time()),
    )


async def _arguments(command: CliCommand, request: Request) -> JsonObject:
    """把 body 校验成 handler 的关键字参数。

    没传的必填项与多余的参数都当不合法回 400 —— 静默丢掉多余参数的话，命令拼错名字
    就会跑出一个「成功但答非所问」的结果。
    """
    try:
        payload = await request.json()
    except ValueError:
        raise BadQuery("body 不是 JSON") from None
    if not isinstance(payload, dict):
        raise BadQuery("body 必须是对象")
    arguments = {name: _typed(name, command, value) for name, value in payload.items()}
    _validate(command, arguments)
    return arguments


def _typed(name: str, command: CliCommand, value: JsonValue) -> JsonValue:
    """按声明把文本换成该有的类型。

    调用方是命令行 —— `oath-cli --limit=2` 过来的就是字符串 `"2"`。换不过去是这条命令
    的用法错了，回 400，不让 handler 收下一个类型不对的值。已经是对的类型就原样放过，
    直接发 JSON 的调用方不必绕这一圈。
    """
    if not isinstance(value, str):
        return value
    declared = command.parameters.get("properties", {})
    spec = declared.get(name) if isinstance(declared, dict) else None
    kind = spec.get("type") if isinstance(spec, dict) else None
    try:
        if kind == "integer":
            return int(value)
        if kind == "number":
            return float(value)
    except ValueError:
        raise BadQuery(f"{name} 不是数字") from None
    if kind == "boolean":
        if value not in ("true", "false"):
            raise BadQuery(f"{name} 只能是 true 或 false")
        return value == "true"
    return value


def _validate(command: CliCommand, arguments: JsonObject) -> None:
    """按声明校验 body：必填、拼错、多余、类型不对都在这里回 400。"""
    errors = sorted(Draft202012Validator(command.parameters).iter_errors(arguments), key=str)
    if errors:
        first: ValidationError = errors[0]
        path = ".".join(str(part) for part in first.absolute_path) or "参数"
        raise BadQuery(f"{path} {first.message}")


__all__ = ["CliCommand", "cli_routes"]
