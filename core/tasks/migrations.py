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
    if version == [2]:
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
