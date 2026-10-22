CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY CHECK(version=1));
INSERT OR IGNORE INTO schema_version VALUES(1);
CREATE TABLE IF NOT EXISTS users (
 user_id TEXT PRIMARY KEY, journal_seq INTEGER NOT NULL DEFAULT 0,
 event_seq INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0,
 state_json TEXT NOT NULL CHECK(json_valid(state_json)), next_invoke REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS journal_events (
 user_id TEXT NOT NULL REFERENCES users(user_id), seq INTEGER NOT NULL,
 idempotency_key TEXT NOT NULL, record_json TEXT NOT NULL CHECK(json_valid(record_json)),
 PRIMARY KEY(user_id,seq), UNIQUE(user_id,idempotency_key)
);
CREATE TABLE IF NOT EXISTS commits (
 user_id TEXT NOT NULL REFERENCES users(user_id), commit_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL, PRIMARY KEY(user_id,commit_id)
);
CREATE TABLE IF NOT EXISTS request_archives (
 user_id TEXT NOT NULL REFERENCES users(user_id), digest TEXT NOT NULL,
 body BLOB NOT NULL, raw_bytes INTEGER NOT NULL,
 PRIMARY KEY(user_id,digest)
);
CREATE TABLE IF NOT EXISTS broker_outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(user_id),
 kind TEXT NOT NULL CHECK(kind IN ('user','tool','system')), identity TEXT NOT NULL,
 item_json TEXT NOT NULL CHECK(json_valid(item_json)), fingerprint TEXT NOT NULL,
 sent INTEGER NOT NULL DEFAULT 0 CHECK(sent IN (0,1)),
 UNIQUE(user_id,identity)
);
CREATE INDEX IF NOT EXISTS broker_pending ON broker_outbox(sent,id);
CREATE TABLE IF NOT EXISTS inbox_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(user_id),
 kind TEXT NOT NULL CHECK(kind IN ('user','tool','system')), identity TEXT NOT NULL,
 item_json TEXT NOT NULL CHECK(json_valid(item_json)),
 UNIQUE(user_id,identity)
);
CREATE INDEX IF NOT EXISTS inbox_user ON inbox_events(user_id,kind,id);
CREATE TABLE IF NOT EXISTS leases (
 user_id TEXT NOT NULL REFERENCES users(user_id), lane TEXT NOT NULL,
 owner TEXT NOT NULL, token INTEGER NOT NULL, expires REAL NOT NULL, touched REAL NOT NULL,
 PRIMARY KEY(user_id,lane)
);
CREATE TABLE IF NOT EXISTS scheduling (
 user_id TEXT PRIMARY KEY REFERENCES users(user_id), requested INTEGER NOT NULL DEFAULT 0,
 completed INTEGER NOT NULL DEFAULT 0, claimed INTEGER NOT NULL DEFAULT 0,
 ready_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ready_users ON scheduling(ready_at,requested,completed);
CREATE TABLE IF NOT EXISTS steps (
 user_id TEXT NOT NULL REFERENCES users(user_id), step_id TEXT NOT NULL, run_id TEXT NOT NULL,
 status TEXT NOT NULL, body_json TEXT NOT NULL CHECK(json_valid(body_json)),
 PRIMARY KEY(user_id,step_id)
);
CREATE INDEX IF NOT EXISTS steps_status ON steps(status,user_id);
CREATE TABLE IF NOT EXISTS tool_invocations (
 user_id TEXT NOT NULL REFERENCES users(user_id), invocation_id TEXT NOT NULL,
 batch_id TEXT NOT NULL, status TEXT NOT NULL, deadline REAL,
 body_json TEXT NOT NULL CHECK(json_valid(body_json)), PRIMARY KEY(user_id,invocation_id)
);
CREATE INDEX IF NOT EXISTS calls_status ON tool_invocations(status,user_id);
CREATE TABLE IF NOT EXISTS tool_batches (
 user_id TEXT NOT NULL REFERENCES users(user_id), batch_id TEXT NOT NULL,
 body_json TEXT NOT NULL CHECK(json_valid(body_json)), PRIMARY KEY(user_id,batch_id)
);
CREATE TABLE IF NOT EXISTS outbox_events (
 user_id TEXT NOT NULL REFERENCES users(user_id), seq INTEGER NOT NULL,
 idempotency_key TEXT NOT NULL, type TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 created_at TEXT NOT NULL, PRIMARY KEY(user_id,seq), UNIQUE(user_id,idempotency_key)
);
CREATE TABLE IF NOT EXISTS client_cursors (
 user_id TEXT NOT NULL REFERENCES users(user_id), client_id TEXT NOT NULL,
 acked INTEGER NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0,
 generation INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(user_id,client_id), CHECK(acked>=0 AND delivered>=acked)
);
CREATE TABLE IF NOT EXISTS trace_spans (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(user_id),
 trace_id TEXT NOT NULL, span_id TEXT NOT NULL, body_json TEXT NOT NULL CHECK(json_valid(body_json))
);
CREATE INDEX IF NOT EXISTS trace_lookup ON trace_spans(user_id,trace_id,id);
CREATE TABLE IF NOT EXISTS result_details (
 user_id TEXT NOT NULL REFERENCES users(user_id), detail_id TEXT NOT NULL,
 content TEXT NOT NULL, PRIMARY KEY(user_id,detail_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(user_id),
 body_json TEXT NOT NULL CHECK(json_valid(body_json))
);

CREATE TABLE IF NOT EXISTS relay_lease (
 id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL, token INTEGER NOT NULL, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_checkpoints (
 user_id TEXT PRIMARY KEY REFERENCES users(user_id), seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_events (
 user_id TEXT NOT NULL REFERENCES users(user_id), seq INTEGER NOT NULL,
 type TEXT NOT NULL, entity TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(user_id,seq)
);
CREATE INDEX IF NOT EXISTS conversation_entities ON conversation_events(user_id,type,entity);
CREATE TABLE IF NOT EXISTS conversation_streams (
 user_id TEXT NOT NULL REFERENCES users(user_id), step_id TEXT NOT NULL, seq INTEGER NOT NULL,
 attempt INTEGER NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(user_id,step_id)
);

CREATE TABLE IF NOT EXISTS subagents (
 agent_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
 storage_id TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL,
 title TEXT NOT NULL, objective TEXT NOT NULL, success_criteria TEXT NOT NULL,
 model TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '',
 max_steps INTEGER NOT NULL, deadline REAL NOT NULL, created_at REAL NOT NULL,
 finished_at REAL, UNIQUE(user_id,request_id)
);
CREATE INDEX IF NOT EXISTS subagents_owner ON subagents(user_id,created_at);
CREATE INDEX IF NOT EXISTS subagents_deadline ON subagents(status,deadline);
CREATE TABLE IF NOT EXISTS subagent_notes (
 agent_id TEXT NOT NULL REFERENCES subagents(agent_id), note_id TEXT NOT NULL,
 content TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(agent_id,note_id)
);
