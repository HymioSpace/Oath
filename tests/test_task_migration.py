"""Exercise real on-disk migration and transaction rollback."""

import json
import sqlite3
from pathlib import Path

import pytest

from core.storage.database import TABLES, Database
from core.tasks.migrations import migrate


def test_upgrade_existing_database_preserves_user(tmp_path):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(Path("core/storage/schema.sql").read_text())
        db.execute(
            "INSERT INTO users(user_id,state_json) VALUES(?,?)",
            ("owner", json.dumps({name: {} for name in TABLES})),
        )
    store = Database(path)
    with store.transaction(readonly=True) as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 6
        assert db.execute("SELECT user_id FROM users").fetchone()[0] == "owner"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'task%'")
        assert len(tables.fetchall()) >= 6
    store._owner.close()
    reopened = Database(path)
    with reopened.transaction(readonly=True) as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 6
    reopened._owner.close()


def test_failed_migration_rolls_back_all_tables(tmp_path):
    path = tmp_path / "broken.sqlite"
    db = sqlite3.connect(path, isolation_level=None)
    db.executescript(Path("core/storage/schema.sql").read_text())
    db.execute("CREATE TABLE task_runs(conflict TEXT)")
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError):
        migrate(db)
    db.rollback()
    assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 1
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='tasks'").fetchone()
    db.close()


def test_legacy_import_is_paused_and_replay_safe(tmp_path):
    import importlib.util

    module_spec = importlib.util.spec_from_file_location(
        "migrate_tasks", Path("scripts/migrate_hymio_tasks.py")
    )
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    source = tmp_path / "hymio.sqlite"
    destination = tmp_path / "oath.sqlite"
    with sqlite3.connect(source) as db:
        db.execute(
            "CREATE TABLE tasks(id TEXT,user_id TEXT,title TEXT,objective "
            "TEXT,success_criteria TEXT,schedule TEXT,kind TEXT)"
        )
        db.execute("CREATE TABLE task_runs(id TEXT,task_id TEXT,status TEXT)")
        db.execute(
            "INSERT INTO tasks VALUES(?,?,?,?,?,?,?)",
            (
                "old_task",
                "owner",
                "Reminder",
                "Drink water",
                "Sent",
                json.dumps({"mode": "daily", "time": "20:00", "timezone": "Asia/Shanghai"}),
                "reminder",
            ),
        )
        db.execute("INSERT INTO task_runs VALUES('old_run','old_task','running')")
    assert module.migrate(source, destination)["applied"] is False
    assert not destination.exists()
    assert module.migrate(source, destination, True)["imported_paused"] == 1
    assert module.migrate(source, destination, True)["imported_paused"] == 0
    with sqlite3.connect(destination) as db:
        assert db.execute("SELECT lifecycle FROM tasks").fetchone()[0] == "paused"
        assert (
            db.execute("SELECT count(*) FROM task_events WHERE type='legacy_imported'").fetchone()[
                0
            ]
            == 1
        )
        assert db.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 0


def test_upgrade_task_v2_preserves_existing_task_run(tmp_path):
    from tests._support.storage import Storage
    from tests.test_tasks_e2e import create

    path = tmp_path / "oath.sqlite3"
    service = Storage(path)
    task = create(service, "agent")["task"]
    service.call("task.tick")
    before = service.call("task.get", user_id="u1", task_id=task["task_id"], view="detail")
    service._inner.database._owner.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE task_delegations")
        db.execute("UPDATE schema_version SET version=2")
    restored = Storage(path)
    after = restored.call("task.get", user_id="u1", task_id=task["task_id"], view="detail")
    assert after["run"]["run_id"] == before["run"]["run_id"]
    assert after["run"]["agent_id"] == before["run"]["agent_id"]
    assert after["run"]["status"] == "running"
    with restored.transaction() as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 6
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_upgrade_task_v4_adds_team_messages_and_workflow_nodes(tmp_path):
    from tests._support.storage import Storage
    from tests.test_tasks_e2e import create

    path = tmp_path / "oath.sqlite3"
    service = Storage(path)
    task = create(service, "agent")["task"]
    service._inner.database._owner.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE task_team_messages")
        db.execute("DROP TABLE task_workflow_nodes")
        db.execute("UPDATE schema_version SET version=4")
    restored = Storage(path)
    detail = restored.call("task.get", user_id="u1", task_id=task["task_id"], view="detail")
    assert detail["task"]["task_id"] == task["task_id"]
    with restored.transaction() as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 6
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'task_%'"
            )
        }
        assert {"task_team_messages", "task_workflow_nodes"} <= tables
        columns = {row[1] for row in db.execute("PRAGMA table_info(task_delegations)")}
        assert {"role", "workflow_node_id"} <= columns
        workflow_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='task_workflow_nodes'"
        ).fetchone()[0]
        assert "'paused'" in workflow_sql
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_upgrade_v5_repairs_legacy_budget_pause_workflow_nodes(tmp_path):
    from tests._support.storage import Storage
    from tests.test_task_modes import create_mode, running_actor

    path = tmp_path / "oath.sqlite3"
    service = Storage(path)
    task = create_mode(service, "dynamic_workflow", "legacy_pause_migration")
    actor, run_id = running_actor(service, task["task_id"])
    defined = service.call(
        "workflow.define",
        user_id=actor,
        request_id="define_legacy_pause",
        nodes=[
            {
                "id": "work",
                "title": "执行中节点",
                "objective": "完成工作",
                "success_criteria": "工作完成",
            }
        ],
    )
    agent_id = defined["nodes"][0]["agent_id"]
    paused = service.call(
        "task.pause",
        user_id="u1",
        task_id=task["task_id"],
        expected_revision=task["revision"],
        reason="预算暂停",
        request_id="legacy_pause",
    )
    service._inner.database._owner.close()
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE task_workflow_nodes SET status='pending',started_at=NULL,finished_at=NULL "
            "WHERE run_id=?",
            (run_id,),
        )
        db.execute(
            "UPDATE subagents SET status='failed',result='Owning task pause' WHERE agent_id=?",
            (agent_id,),
        )
        db.execute("UPDATE schema_version SET version=5")

    restored = Storage(path)
    detail = restored.call(
        "task.get", user_id="u1", task_id=task["task_id"], view="detail"
    )
    node = detail["execution"]["workflow"]["nodes"][0]
    agent = next(
        child
        for child in detail["execution"]["subagents"]
        if child["agent_id"] == agent_id
    )
    assert paused["run"]["status"] == "paused"
    assert node["status"] == "paused"
    assert node["started_at"] is not None
    assert agent["status"] == "paused"
    with restored.transaction() as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 6
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_upgrade_v5_repairs_legacy_cancelled_workflow_nodes(tmp_path):
    from tests._support.storage import Storage
    from tests.test_task_modes import create_mode, running_actor

    path = tmp_path / "oath.sqlite3"
    service = Storage(path)
    task = create_mode(service, "dynamic_workflow", "legacy_cancel_migration")
    actor, run_id = running_actor(service, task["task_id"])
    service.call(
        "workflow.define",
        user_id=actor,
        request_id="define_legacy_cancel",
        nodes=[
            {
                "id": "work",
                "title": "已取消节点",
                "objective": "完成工作",
                "success_criteria": "工作完成",
            }
        ],
    )
    paused = service.call(
        "task.pause",
        user_id="u1",
        task_id=task["task_id"],
        expected_revision=task["revision"],
        reason="暂停",
        request_id="legacy_cancel_pause",
    )
    service.call(
        "task.cancel",
        user_id="u1",
        task_id=task["task_id"],
        expected_revision=paused["task"]["revision"],
        reason="取消",
        request_id="legacy_cancel",
    )
    service._inner.database._owner.close()
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE task_workflow_nodes SET status='pending',finished_at=NULL WHERE run_id=?",
            (run_id,),
        )
        db.execute("UPDATE schema_version SET version=5")

    restored = Storage(path)
    detail = restored.call(
        "task.get", user_id="u1", task_id=task["task_id"], view="detail"
    )
    assert detail["execution"]["workflow"]["nodes"][0]["status"] == "cancelled"
    with restored.transaction() as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 6
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
