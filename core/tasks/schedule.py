"""Deterministic, timezone-aware trigger calculation."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def next_due(trigger, after):
    mode = trigger["mode"]
    if mode == "now":
        return after
    if mode == "event":
        return None
    if mode == "once":
        return datetime.fromisoformat(trigger["at"].replace("Z", "+00:00")).timestamp()
    zone = ZoneInfo(trigger["timezone"])
    local = datetime.fromtimestamp(after, zone)
    (hour, minute) = map(int, trigger["time"].split(":"))
    for days in range(9):
        day = local.date() + timedelta(days=days)
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
        stamp = candidate.timestamp()
        if (
            stamp <= after
            or datetime.fromtimestamp(stamp, zone).strftime("%H:%M") != trigger["time"]
        ):
            continue
        if mode == "weekly" and day.weekday() not in trigger["weekdays"]:
            continue
        if (
            trigger.get("end_at")
            and stamp > datetime.fromisoformat(trigger["end_at"].replace("Z", "+00:00")).timestamp()
        ):
            return None
        return stamp
    raise ValueError("Cannot calculate next occurrence")
