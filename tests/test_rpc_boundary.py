"""服务间 RPC 的入口与身份检查。

Storage 的操作集合是闭合的 —— 客户端发 SQL 的路子从 `rpc_app` 这一层就不存在。
这一层只做三件事：验 token、把操作名挡在白名单外、把下层抛的领域错误映射成
调用方能据此决定重试还是放弃的状态码。
"""

from __future__ import annotations

from typing import Any

from starlette.testclient import TestClient

from bootstrap.common.rpc import rpc_app
from core.protocol.interface.storage import LeaseLost, StorageConflict

TOKEN = "t" * 32
OPERATIONS = {"state.read", "lease.acquire"}


class _Service:
    """只记下被调了什么，抛什么由用例指定。"""

    def __init__(self, outcome: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.outcome = outcome

    def call(self, operation: str, **params: Any) -> Any:
        self.calls.append((operation, params))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return {"operation": operation, **params}


def _client(service: _Service, token: str = TOKEN) -> TestClient:
    return TestClient(rpc_app(service, token, OPERATIONS))  # type: ignore[no-untyped-call]


def _post(
    client: TestClient,
    operation: str = "state.read",
    params: dict[str, Any] | None = None,
    token: str | None = TOKEN,
    header: str | None = None,
) -> Any:
    if header is not None:
        headers = {"Authorization": header}
    else:
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return client.post(f"/v1/{operation}", json=params or {}, headers=headers)


# --------------------------------------------------------------------------
# 身份
# --------------------------------------------------------------------------


def test_the_right_token_gets_through() -> None:
    service = _Service()

    assert _post(_client(service)).status_code == 200
    assert service.calls == [("state.read", {})]


def test_no_authorization_header_is_refused() -> None:
    """服务之间没有匿名路径。缺 header 与错 token 同等对待。"""
    service = _Service()

    assert _post(_client(service), token=None).status_code == 401
    assert service.calls == []


def test_a_wrong_token_is_refused() -> None:
    service = _Service()

    assert _post(_client(service), token="x" * 32).status_code == 401
    assert service.calls == []


def test_a_token_that_is_a_prefix_of_the_real_one_is_refused() -> None:
    """凭证要整个对上：短一截的 token 进不来。

    这里验证的是「必须完全匹配」，不是比较方式。恒定时间那部分从用例里看不出来 ——
    `==` 与 `compare_digest` 对定值的结果一模一样，差别只在时序。
    """
    service = _Service()

    assert _post(_client(service), token=TOKEN[:-1]).status_code == 401
    assert _post(_client(service), token="").status_code == 401


def test_the_scheme_is_part_of_the_credential() -> None:
    """裸 token 不算数 —— 凭证是整个 header 值，不是 token 那一段。"""
    service = _Service()

    assert _post(_client(service), header=TOKEN).status_code == 401
    assert _post(_client(service), header=f"bearer {TOKEN}").status_code == 401
    assert _post(_client(service), header=f"Bearer {TOKEN} ").status_code == 401
    assert service.calls == []


def test_the_heartbeat_needs_no_credential() -> None:
    """编排层要能在没有共享密钥时探活 —— 它只答「进程在不在」。"""
    assert _client(_Service()).get("/heartbeat").json() == {"status": "alive"}


# --------------------------------------------------------------------------
# 操作白名单
# --------------------------------------------------------------------------


def test_an_operation_outside_the_closed_set_is_not_reachable() -> None:
    """Storage 不接 SQL。名字不在白名单里，连下层都到不了。"""
    service = _Service()

    response = _post(_client(service), operation="execute_sql")

    assert response.status_code == 404
    assert response.json()["error"] == "unknown_operation"
    assert service.calls == []


# --------------------------------------------------------------------------
# 领域错误 → 状态码
# --------------------------------------------------------------------------


def test_a_lease_lost_answers_412() -> None:
    """租约没了意味着「换个 owner 重试」，不是重试同样的请求。"""
    response = _post(_client(_Service(LeaseLost("gone"))))

    assert response.status_code == 412
    assert response.json()["error"] == "gone"


def test_a_conflict_answers_409() -> None:
    """版本冲突是并发，不是故障：调用方要重读，不能原样重发。"""
    assert _post(_client(_Service(StorageConflict("stale")))).status_code == 409


def test_a_bad_parameter_answers_400() -> None:
    response = _post(_client(_Service(ValueError("Invalid lease lane"))))

    assert response.status_code == 400
    assert response.json()["error"] == "Invalid lease lane"


def test_an_unexpected_failure_does_not_leak_internals() -> None:
    """503 只说「不可用」。异常细节可能带路径、SQL、user_id，不能出服务边界。"""
    response = _post(_client(_Service(RuntimeError("sqlite3: /secret/path"))))

    assert response.status_code == 503
    assert "secret" not in response.text


def test_a_body_that_is_not_an_object_is_refused() -> None:
    client = _client(_Service())

    response = client.post(
        "/v1/state.read", json=[1, 2], headers={"Authorization": f"Bearer {TOKEN}"}
    )

    assert response.status_code == 400


def test_an_oversized_body_is_refused_before_it_is_parsed() -> None:
    """上限比任何真实请求都大一个量级；到这里说明有人在灌东西。"""
    client = _client(_Service())
    payload = {"pad": "x" * (33 * 1024 * 1024)}

    response = client.post(
        "/v1/state.read", json=payload, headers={"Authorization": f"Bearer {TOKEN}"}
    )

    assert response.status_code == 413
