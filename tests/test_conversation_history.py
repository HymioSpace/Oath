import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.storage import conversation_history


def database(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE users(user_id TEXT PRIMARY KEY,event_seq INTEGER NOT NULL);
        CREATE TABLE conversation_checkpoints(user_id TEXT PRIMARY KEY,seq INTEGER NOT NULL);
        CREATE TABLE conversation_events(
            user_id TEXT,seq INTEGER,type TEXT,entity TEXT,payload_json TEXT,created_at TEXT,
            PRIMARY KEY(user_id,seq));
        CREATE TABLE conversation_streams(
            user_id TEXT,step_id TEXT,seq INTEGER,attempt INTEGER,content TEXT,created_at TEXT);
        CREATE TABLE tool_invocations(
            user_id TEXT,invocation_id TEXT,batch_id TEXT,status TEXT);
        CREATE TABLE steps(user_id TEXT,step_id TEXT,status TEXT);
        """
    )
    return db


class ConversationHistoryTests(unittest.TestCase):
    def test_read_only_page_does_not_attempt_checkpoint_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.sqlite"
            db = database(path)
            db.execute("INSERT INTO users VALUES('alice',25)")
            db.execute("INSERT INTO conversation_checkpoints VALUES('alice',25)")
            for seq in range(1, 26):
                db.execute(
                    "INSERT INTO conversation_events VALUES(?,?,?,?,?,?)",
                    (
                        "alice",
                        seq,
                        "user.message",
                        "",
                        '{"content":"hello"}',
                        "2026-01-01T00:00:00Z",
                    ),
                )
            db.commit()
            db.close()

            readonly = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            readonly.row_factory = sqlite3.Row
            readonly.execute("PRAGMA query_only=ON")
            page = conversation_history.page(readonly, "alice", limit=10)
            readonly.close()

        self.assertTrue(page["has_more"])
        self.assertIsNotNone(page["before_cursor"])
        self.assertEqual(page["checkpoint"], 25)

    def test_checkpoint_backfill_projects_existing_outbox_events(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.sqlite"
            db = database(path)
            db.execute("INSERT INTO users VALUES('alice',2)")
            db.execute(
                "CREATE TABLE outbox_events("
                "user_id TEXT,seq INTEGER,type TEXT,payload_json TEXT,created_at TEXT)"
            )
            db.execute(
                "INSERT INTO outbox_events VALUES(?,?,?,?,?)",
                (
                    "alice",
                    1,
                    "user.message",
                    '{"content":"hello","user_context":{"hymio_message_id":"m1"}}',
                    "2026-01-01T00:00:00Z",
                ),
            )
            db.execute(
                "INSERT INTO outbox_events VALUES(?,?,?,?,?)",
                (
                    "alice",
                    2,
                    "assistant.message",
                    '{"step_id":"s1","content":"world","tool_calls":[]}',
                    "2026-01-01T00:00:01Z",
                ),
            )
            db.commit()

            self.assertEqual(conversation_history.ensure(db, "alice"), 2)
            page = conversation_history.page(db, "alice", limit=10)
            db.close()

        self.assertEqual(
            [event["event_type"] for event in page["events"]],
            [
                "user.message",
                "assistant.message",
            ],
        )
