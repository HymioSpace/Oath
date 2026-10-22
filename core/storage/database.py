"""单机 SQLite owner。所有跨表状态转移在短事务中完成。"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from core.observability.metrics import STORAGE_LOCK_WAIT, STORAGE_TRANSACTIONS
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
        from core.storage import conversation_history
        from core.storage.request_archives import compact_existing

        with self.transaction(readonly=True) as db:
            users = [row[0] for row in db.execute("SELECT user_id FROM users")]
        for user_id in users:
            with self.transaction() as db:
                compact_existing(db, user_id)
                conversation_history.ensure(db, user_id)

    @contextmanager
    def transaction(self, *, readonly=False):
        started = time.monotonic()
        mode = "read" if readonly else "write"
        outcome = "error"
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            if not readonly:
                db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA cache_size=-1024")
            db.execute("PRAGMA temp_store=FILE")
            if readonly:
                db.execute("PRAGMA query_only=ON")
            lock_started = time.monotonic()
            try:
                db.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
            finally:
                STORAGE_LOCK_WAIT.labels(mode).observe(time.monotonic() - lock_started)
            yield db
            db.commit()
            outcome = "ok"
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                outcome = "busy"
            db.rollback()
            raise
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
            STORAGE_TRANSACTIONS.labels(mode, outcome).observe(time.monotonic() - started)

    @contextmanager
    def reader(self, user_id=None):
        with self.transaction(readonly=True) as db:
            if (
                user_id is None
                or db.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
            ):
                if user_id is not None:
                    self.validate_identity(db, user_id)
                yield db
                return
        # First access still creates an empty user, outside the read snapshot.
        with self.transaction() as db:
            self.ensure(db, user_id)
        with self.reader(user_id) as db:
            yield db

    @staticmethod
    def validate_identity(db, user_id):
        identity = User.from_storage_id(user_id)
        if (
            identity.agent_id
            and not db.execute(
                "SELECT 1 FROM subagents WHERE storage_id=? AND user_id=?",
                (user_id, identity.user_id),
            ).fetchone()
        ):
            raise ValueError("Unknown delegated execution")

    @staticmethod
    def ensure(db, user_id):
        Database.validate_identity(db, user_id)
        db.execute(
            "INSERT OR IGNORE INTO users(user_id,state_json) VALUES(?,?)",
            (user_id, encode({name: {} for name in TABLES})),
        )
        db.execute(
            "INSERT OR IGNORE INTO conversation_checkpoints(user_id,seq) VALUES(?,0)",
            (user_id,),
        )
        db.execute("INSERT OR IGNORE INTO scheduling(user_id) VALUES(?)", (user_id,))
