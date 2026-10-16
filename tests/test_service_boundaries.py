"""模块边界：谁拥有谁、谁能读到谁的目录、凭证只认到哪一个 scope。"""

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from bootstrap.agent.scope_lock import ScopeBusy, ScopeLock
from bootstrap.biz.assembly import build_application
from bootstrap.biz.server import build_app
from bootstrap.common.extension_loader import load_extensions
from bootstrap.manager import SessionEntry, SessionManager
from core.protocol.datatype.config import OathConfig
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.trace import Span, SpanStatus, new_trace_id
from core.protocol.error import PermissionDeniedError
from core.trace.impl import FileTraceStore

TOKEN = "synthetic-service-token"
SCOPE = Scope("u", "s")
SPAN = Span(
    trace_id=new_trace_id(),
    span_id="0000000000000001",
    name="invoke_agent",
    start_unix_nano=1_000_000,
    end_unix_nano=3_000_000,
    status=SpanStatus.OK,
)


class OfflineManager(SessionManager):
    """登记 session 但不拉起进程 —— 这里验的是边界与路由，不是进程模型。"""

    def _launch(self, entry: SessionEntry, *, wait: bool = True) -> None:
        pass


def config_of(tmp_path: Path) -> OathConfig:
    return OathConfig(data_root=tmp_path, service_credentials=(TOKEN,))


# --------------------------------------------------------------------------
# scope 目录的写权限
# --------------------------------------------------------------------------


def test_one_writer_per_scope_directory(tmp_path: Path) -> None:
    with ScopeLock(tmp_path), pytest.raises(ScopeBusy):
        ScopeLock(tmp_path).acquire()
    with ScopeLock(tmp_path):
        pass


def test_two_scopes_do_not_block_each_other(tmp_path: Path) -> None:
    """锁按 scope 目录走，不是全局一把 —— 两个 agent 各写各的。"""
    with ScopeLock(tmp_path / "u1" / "s1"), ScopeLock(tmp_path / "u1" / "s2"):
        pass


def test_the_lock_is_released_with_the_scope(tmp_path: Path) -> None:
    with ScopeLock(tmp_path):
        pass

    with ScopeLock(tmp_path):
        pass


# --------------------------------------------------------------------------
# 凭证
# --------------------------------------------------------------------------


def test_a_session_credential_resolves_to_exactly_one_scope(tmp_path: Path) -> None:
    manager = OfflineManager(config_of(tmp_path), "")
    issued = manager.create_session("u1")

    identity = manager.authenticate(str(issued["credential"]))

    assert identity.user_id == "u1"
    assert identity.session_id == issued["session_id"]


def test_an_unissued_credential_is_refused(tmp_path: Path) -> None:
    manager = OfflineManager(config_of(tmp_path), "")

    with pytest.raises(PermissionDeniedError):
        manager.authenticate("never-issued")


def test_a_service_credential_is_not_a_session_credential(tmp_path: Path) -> None:
    """两级凭证不通用：服务级那把能建 session，不能冒充某个 session。"""
    manager = OfflineManager(config_of(tmp_path), "")
    manager.create_session("u1")

    with pytest.raises(PermissionDeniedError):
        manager.authenticate(TOKEN)


def test_no_credential_values_reach_the_serialised_config(tmp_path: Path) -> None:
    config = OathConfig(data_root=tmp_path, service_credentials=(TOKEN,))

    rendered = config.to_json()

    assert TOKEN not in str(rendered)
    assert "credential" not in rendered


# --------------------------------------------------------------------------
# 路由与依赖
# --------------------------------------------------------------------------


def test_biz_serves_traces_without_any_agent(tmp_path: Path) -> None:
    """Trace 是 biz 自己读的文件。agent 进程不在，trace 查询照常答。"""
    config = config_of(tmp_path)
    manager = OfflineManager(config, "")
    issued = manager.create_session("u")
    scope = manager.authenticate(str(issued["credential"]))
    FileTraceStore(tmp_path / "traces").append(Scope(scope.user_id, scope.session_id), SPAN)
    auth = {"Authorization": f"Bearer {issued['credential']}"}
    with TestClient(build_app(build_application(config, manager), manager, config)) as client:
        assert client.get("/heartbeat").status_code == 200
        result = client.get("/list_traces", headers=auth)

    assert result.status_code == 200
    assert result.json()["trace_ids"] == [
        {"trace_id": SPAN.trace_id, "span_count": 1, "step_count": 1, "duration_ms": 2.0}
    ]
    # biz 不会因为查 trace 去动 journal。
    assert not (tmp_path / "journal").exists()


def test_session_routes_require_a_service_credential(tmp_path: Path) -> None:
    config = config_of(tmp_path)
    manager = OfflineManager(config, "")
    with TestClient(build_app(build_application(config, manager), manager, config)) as client:
        assert client.get("/list_sessions").status_code == 401
        unauthenticated = client.request("POST", "/create_session", json={"user_id": "u1"})
        assert unauthenticated.status_code == 401
        authorised = client.get("/list_sessions", headers={"Authorization": f"Bearer {TOKEN}"})
        assert authorised.status_code == 200


def test_heartbeat_needs_no_identity(tmp_path: Path) -> None:
    """存活探测问的是「这个 uvicorn 还在处理请求吗」，不是「谁在问」。"""
    config = config_of(tmp_path)
    manager = OfflineManager(config, "")
    with TestClient(build_app(build_application(config, manager), manager, config)) as client:
        assert client.get("/heartbeat").json() == {"status": "alive"}


def test_an_unusable_session_request_is_refused(tmp_path: Path) -> None:
    config = config_of(tmp_path)
    manager = OfflineManager(config, "")
    auth = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(build_app(build_application(config, manager), manager, config)) as client:
        assert client.post("/create_session", json={}, headers=auth).status_code == 400
        assert (
            client.post("/create_session", json={"user_id": "../etc"}, headers=auth).status_code
            == 400
        )


def test_the_session_limit_is_reported_not_queued(tmp_path: Path) -> None:
    config = OathConfig(data_root=tmp_path, service_credentials=(TOKEN,))
    config = replace(config, manager=replace(config.manager, max_sessions=1))
    manager = OfflineManager(config, "")
    auth = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(build_app(build_application(config, manager), manager, config)) as client:
        assert (
            client.post("/create_session", json={"user_id": "u1"}, headers=auth).status_code == 200
        )
        full = client.post("/create_session", json={"user_id": "u2"}, headers=auth)

    assert full.status_code == 503
    assert full.json()["error"] == "session_limit"


def test_biz_takes_only_the_extensions_it_owns(tmp_path: Path) -> None:
    """biz 要 cli，agent 要 tools 与 skills。装错的那类在装配期就不该被读进来。"""
    (tmp_path / "tools").mkdir()
    (tmp_path / "cli").mkdir()
    (tmp_path / "tools" / "broken.py").write_text('raise RuntimeError("agent only")')

    assert load_extensions((tmp_path,), kinds=frozenset({"cli"})).tools == ()
    (tmp_path / "tools" / "broken.py").unlink()
    (tmp_path / "cli" / "broken.py").write_text('raise RuntimeError("biz only")')

    assert load_extensions((tmp_path,), kinds=frozenset({"tools", "skills"})).commands == ()


# --------------------------------------------------------------------------
# 依赖方向
# --------------------------------------------------------------------------


@pytest.mark.parametrize("module", ["bootstrap.supervisor.watchdog", "bootstrap.manager"])
def test_supervision_does_not_import_the_agent_owner(module: str) -> None:
    """supervisor 与 manager 都不认识 `Agent` —— 它们的接口只有 scope 与一条管道。"""
    subprocess.run(
        [
            sys.executable,
            "-c",
            f"import {module}, sys; "
            "assert 'bootstrap.agent.agent' not in sys.modules; "
            "assert 'core.agent.brain' not in sys.modules",
        ],
        check=True,
    )


def test_core_does_not_import_bootstrap() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import core.agent, core.consumer, core.intake.coordinator, sys; "
            "assert not [m for m in sys.modules if m.startswith('bootstrap')]",
        ],
        check=True,
    )
