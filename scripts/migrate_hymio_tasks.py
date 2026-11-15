"""Offline migration of legacy Hymio tasks into Oath.

Default is inspection only. Stop both schedulers and back up both databases before
using --apply. Imported tasks are paused so unknown external effects cannot replay.
Legacy execution history is retained in an immutable migration event.
"""

import argparse
import json
import sqlite3
from pathlib import Path

from core.storage.database import Database
from core.storage.service import StorageService
from core.tasks.storage import call, event, owned


def migrate(source, destination, apply=False):
    source = Path(source).resolve()
    if not source.is_file():
        raise ValueError("Source database not found")
    db = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute("SELECT * FROM tasks")]
    if not apply:
        db.close()
        return {"tasks": len(rows), "applied": False}
    service = StorageService(Database(Path(destination)))
    migrated = 0
    try:
        for row in rows:
            legacy_schedule = json.loads(row["schedule"])
            mode = legacy_schedule["mode"]
            trigger = {
                k: legacy_schedule[k]
                for k in ("mode", "timezone", "at", "time")
                if k in legacy_schedule
            }
            if mode == "weekly":
                trigger["weekdays"] = [legacy_schedule.get("weekday", 0)]
            budget = {
                "max_steps": 100,
                "max_tokens": 100000,
                "max_active_seconds": 3600,
                "max_cost_microusd": None,
            }
            reminder = row.get("kind") == "reminder"
            data = {
                "intent_key": "legacy:" + row["id"],
                "title": row["title"],
                "objective": row["objective"],
                "completion": "maintain" if mode in {"daily", "weekly"} else "achieve",
                "acceptance": {
                    "criteria": row["success_criteria"],
                    "review": "automatic",
                    "require_delivery": True,
                },
                "trigger": trigger,
                "executor": {
                    "kind": "program",
                    "action": "notify",
                    "params": {"text": row["objective"]},
                }
                if reminder
                else {"kind": "agent", "tool_profile": "default", "model_profile": "default"},
                "context": {
                    "brief": "Imported from legacy Hymio; review history before resuming.",
                    "refs": [],
                },
                "plan": [],
                "policy": {
                    "run_budget": budget,
                    "task_budget": {
                        "window": "day" if mode in {"daily", "weekly"} else "lifetime",
                        "timezone": legacy_schedule.get("timezone", "UTC"),
                        "limits": budget,
                    },
                    "notify": {"mode": "result", "destination_ref": "channel_current"},
                    "overlap": "skip",
                    "misfire": "skip",
                    "grace_seconds": legacy_schedule.get("grace_seconds", 900),
                    "run_deadline_seconds": legacy_schedule.get("run_timeout_seconds", 3600),
                    "max_retries": 2,
                },
            }
            with service.database.transaction() as target:
                Database.ensure(target, row["user_id"])
                made = call(
                    target,
                    row["user_id"],
                    "task.create",
                    {"user_id": row["user_id"], "request_id": "import:" + row["id"], **data},
                )
                tid = made["task"]["task_id"]
                if made["replayed"]:
                    continue
                call(
                    target,
                    row["user_id"],
                    "task.pause",
                    {
                        "user_id": row["user_id"],
                        "task_id": tid,
                        "expected_revision": 1,
                        "reason": (
                            "Review history before resuming; old scheduler must remain stopped"
                        ),
                        "request_id": "import_pause:" + row["id"],
                    },
                )
                history = [
                    dict(r)
                    for r in db.execute("SELECT * FROM task_runs WHERE task_id=?", (row["id"],))
                ]
                event(
                    target,
                    owned(target, row["user_id"], tid),
                    "legacy_imported",
                    {"legacy_task": row, "legacy_runs": history, "source": "hymio-v1"},
                )
                target.execute("UPDATE tasks SET needs_attention=1 WHERE task_id=?", (tid,))
            migrated += 1
    finally:
        db.close()
        service.database._owner.close()
    return {"tasks": len(rows), "imported_paused": migrated, "applied": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(args.source, args.database, args.apply)))


if __name__ == "__main__":
    main()
