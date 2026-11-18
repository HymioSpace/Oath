-- 增量表；依赖 Oath 现有 users / subagents 表。
CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(user_id),
  intent_key TEXT NOT NULL,
  parent_task_id TEXT,
  paused_by_task_id TEXT,
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
  lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active','paused','closed')),
  close_reason TEXT CHECK(close_reason IN ('achieved','cancelled','expired')),
  title TEXT NOT NULL,
  objective TEXT NOT NULL,
  completion TEXT NOT NULL CHECK(completion IN ('achieve','maintain')),
  acceptance_json TEXT NOT NULL CHECK(json_valid(acceptance_json)),
  plan_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(plan_json)),
  context_seed_json TEXT NOT NULL CHECK(json_valid(context_seed_json)),
  trigger_json TEXT NOT NULL CHECK(json_valid(trigger_json)),
  executor_json TEXT NOT NULL CHECK(json_valid(executor_json)),
  policy_json TEXT NOT NULL CHECK(json_valid(policy_json)),
  memory_json TEXT NOT NULL DEFAULT '{"summary":"","refs":[]}' CHECK(json_valid(memory_json)),
  needs_attention INTEGER NOT NULL DEFAULT 0 CHECK(needs_attention IN (0,1)),
  next_due_at REAL,
  next_review_at REAL,
  event_cursor INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(user_id, task_id), UNIQUE(user_id, intent_key),
  FOREIGN KEY(user_id, parent_task_id) REFERENCES tasks(user_id, task_id),
  FOREIGN KEY(user_id, paused_by_task_id) REFERENCES tasks(user_id, task_id),
  CHECK(parent_task_id IS NULL OR parent_task_id != task_id),
  CHECK((lifecycle = 'closed') = (close_reason IS NOT NULL)),
  CHECK((lifecycle = 'paused') = (paused_by_task_id IS NOT NULL))
);
CREATE INDEX tasks_owner ON tasks(user_id, lifecycle, updated_at DESC, task_id);
CREATE INDEX tasks_due ON tasks(next_due_at) WHERE lifecycle = 'active';
CREATE INDEX tasks_review ON tasks(next_review_at) WHERE lifecycle = 'active';
CREATE INDEX tasks_parent ON tasks(user_id, parent_task_id);
CREATE INDEX tasks_trigger ON tasks(user_id,
  json_extract(trigger_json, '$.source'), json_extract(trigger_json, '$.key'));

CREATE TABLE task_runs (
  run_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  task_revision INTEGER NOT NULL,
  spec_snapshot_json TEXT NOT NULL CHECK(json_valid(spec_snapshot_json)),
  occurrence_key TEXT NOT NULL,
  retry_of_run_id TEXT,
  status TEXT NOT NULL CHECK(status IN
    ('queued','running','waiting','paused','succeeded','failed','cancelled')),
  stage TEXT NOT NULL DEFAULT 'execute' CHECK(stage IN ('execute','verify','deliver')),
  version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
  control_epoch INTEGER NOT NULL DEFAULT 1 CHECK(control_epoch > 0),
  agent_id TEXT REFERENCES subagents(agent_id),
  context_id TEXT REFERENCES users(user_id),
  progress_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(progress_json)),
  wait_json TEXT CHECK(wait_json IS NULL OR json_valid(wait_json)),
  resume_status TEXT CHECK(resume_status IN ('queued','running','waiting')),
  pending_revision INTEGER,
  pending_spec_json TEXT CHECK(pending_spec_json IS NULL OR json_valid(pending_spec_json)),
  result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
  error_json TEXT CHECK(error_json IS NULL OR json_valid(error_json)),
  worker_state_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(worker_state_json)),
  usage_json TEXT NOT NULL DEFAULT '{"steps":0,"tokens":0,"active_seconds":0,"cost_microusd":0}' CHECK(json_valid(usage_json)),
  reserved_usage_json TEXT NOT NULL DEFAULT '{"steps":0,"tokens":0,"active_seconds":0,"cost_microusd":0}' CHECK(json_valid(reserved_usage_json)),
  signal_floor INTEGER NOT NULL DEFAULT 0,
  due_at REAL NOT NULL,
  next_check_at REAL,
  deadline_at REAL,
  last_progress_at REAL,
  lease_owner TEXT,
  lease_token INTEGER NOT NULL DEFAULT 0,
  lease_until REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL,
  UNIQUE(user_id, run_id), UNIQUE(user_id, task_id, run_id),
  UNIQUE(task_id, occurrence_key),
  FOREIGN KEY(user_id, task_id) REFERENCES tasks(user_id, task_id),
  FOREIGN KEY(user_id, task_id, retry_of_run_id)
    REFERENCES task_runs(user_id, task_id, run_id),
  CHECK((pending_revision IS NULL) = (pending_spec_json IS NULL)),
  CHECK(status != 'waiting' OR wait_json IS NOT NULL),
  CHECK((status IN ('succeeded','failed','cancelled')) = (finished_at IS NOT NULL)),
  CHECK((agent_id IS NULL) = (context_id IS NULL))
);
CREATE UNIQUE INDEX task_one_open_run ON task_runs(task_id)
  WHERE status IN ('queued','running','waiting','paused');
CREATE UNIQUE INDEX task_run_context ON task_runs(context_id) WHERE context_id IS NOT NULL;
CREATE INDEX task_runs_owner ON task_runs(user_id, task_id, created_at DESC, run_id);
CREATE INDEX task_runs_ready ON task_runs(status, due_at, lease_until);
CREATE INDEX task_runs_check ON task_runs(next_check_at)
  WHERE status IN ('queued','running','waiting');
CREATE INDEX task_runs_lease ON task_runs(lease_until) WHERE status = 'running';

CREATE TABLE task_signals (
  signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(user_id),
  source TEXT NOT NULL,
  event_key TEXT NOT NULL,
  correlation_key TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  occurred_at REAL NOT NULL,
  received_at REAL NOT NULL,
  UNIQUE(user_id, signal_id), UNIQUE(user_id, source, event_key)
);
CREATE INDEX task_signals_match ON task_signals(user_id, source, correlation_key, signal_id);

CREATE TABLE task_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  run_id TEXT,
  signal_id INTEGER,
  event_key TEXT NOT NULL,
  type TEXT NOT NULL,
  task_revision INTEGER NOT NULL,
  run_version INTEGER,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  created_at REAL NOT NULL,
  UNIQUE(user_id, event_key), UNIQUE(run_id, signal_id),
  FOREIGN KEY(user_id, task_id) REFERENCES tasks(user_id, task_id),
  FOREIGN KEY(user_id, task_id, run_id) REFERENCES task_runs(user_id, task_id, run_id),
  FOREIGN KEY(user_id, signal_id) REFERENCES task_signals(user_id, signal_id)
);
CREATE INDEX task_events_history ON task_events(user_id, task_id, event_id);
CREATE INDEX task_events_usage ON task_events(user_id, type, created_at);

CREATE TABLE task_requests (
  user_id TEXT NOT NULL REFERENCES users(user_id),
  request_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  response_json TEXT NOT NULL CHECK(json_valid(response_json)),
  created_at REAL NOT NULL,
  PRIMARY KEY(user_id, request_id)
);

CREATE TABLE task_deliveries (
  delivery_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  purpose TEXT NOT NULL,
  destination_ref TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  status TEXT NOT NULL CHECK(status IN
    ('pending','inflight','accepted','unknown','failed','cancelled')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL,
  lease_owner TEXT,
  lease_token INTEGER NOT NULL DEFAULT 0,
  lease_until REAL NOT NULL DEFAULT 0,
  receipt_json TEXT CHECK(receipt_json IS NULL OR json_valid(receipt_json)),
  created_at REAL NOT NULL,
  accepted_at REAL,
  UNIQUE(user_id, dedupe_key),
  FOREIGN KEY(user_id, task_id, run_id) REFERENCES task_runs(user_id, task_id, run_id)
);
CREATE INDEX task_deliveries_ready ON task_deliveries(status, next_attempt_at, lease_until);
