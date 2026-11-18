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
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 3
        assert db.execute("SELECT user_id FROM users").fetchone()[0] == "owner"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'task%'")
        assert len(tables.fetchall()) >= 6
    store._owner.close()
    reopened = Database(path)
    with reopened.transaction(readonly=True) as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 3
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
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 3
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
