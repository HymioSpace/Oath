"""Atomic task schema migration under the Storage owner's lock."""

import sqlite3
from pathlib import Path


def execute_script(db, script):
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("Incomplete migration statement")


def migrate(db):
    version = [row[0] for row in db.execute("SELECT version FROM schema_version")]
    if version == [3]:
        return
    if version == [2]:
        upgrade_delegation(db)
        return
    if version != [1]:
        raise RuntimeError("Unsupported database schema")
    execute_script(db, Path(__file__).with_name("schema.sql").read_text())
    db.execute("ALTER TABLE schema_version RENAME TO schema_version_v1")
    db.execute(
        "CREATE TABLE schema_version(id INTEGER PRIMARY KEY CHECK(id=1), "
        "version INTEGER NOT NULL CHECK(version>=1))"
    )
    db.execute("INSERT INTO schema_version VALUES(1,2)")
    db.execute("DROP TABLE schema_version_v1")
    upgrade_delegation(db)


def upgrade_delegation(db):
    execute_script(
        db,
        """
CREATE TABLE task_delegations (
 user_id TEXT NOT NULL, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
 parent_agent_id TEXT NOT NULL REFERENCES subagents(agent_id),
 agent_id TEXT PRIMARY KEY REFERENCES subagents(agent_id),
 state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','paused','closed')),
 created_at REAL NOT NULL,
 FOREIGN KEY(user_id,task_id,run_id) REFERENCES task_runs(user_id,task_id,run_id)
);
CREATE INDEX task_delegations_parent ON task_delegations(parent_agent_id,created_at);
CREATE INDEX task_delegations_run ON task_delegations(run_id);
""",
    )
    db.execute("UPDATE schema_version SET version=3 WHERE id=1")
