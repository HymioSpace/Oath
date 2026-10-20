"""单机 SQLite owner。所有跨表状态转移在短事务中完成。"""

from __future__ import annotations

import fcntl
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from core.protocol.datatype.user import User

TABLES = ("inputs", "batches", "calls", "steps", "meta", "conversation", "controls", "compactions")


def encode(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path.resolve()
        self._owner = self.path.with_suffix(".owner.lock").open("a")
        try:
            fcntl.flock(self._owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._owner.close()
            raise RuntimeError("A Storage service already owns this SQLite database") from None
        with self.transaction() as db:
            db.executescript(Path(__file__).with_name("schema.sql").read_text())
            if [r[0] for r in db.execute("SELECT version FROM schema_version")] != [1]:
                raise RuntimeError("Unsupported database schema")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def ensure(db, user_id):
        identity = User.from_storage_id(user_id)
        if (
            identity.agent_id
            and not db.execute(
                "SELECT 1 FROM subagents WHERE storage_id=? AND user_id=?",
                (user_id, identity.user_id),
            ).fetchone()
        ):
            raise ValueError("Unknown delegated execution")
        db.execute(
            "INSERT OR IGNORE INTO users(user_id,state_json) VALUES(?,?)",
            (user_id, encode({name: {} for name in TABLES})),
        )
        db.execute("INSERT OR IGNORE INTO scheduling(user_id) VALUES(?)", (user_id,))
