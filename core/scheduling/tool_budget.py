"""Durable limit for consecutive steps driven only by tool results."""

from core.journal.runtime_store import State


def exhausted(state: State, limit: int) -> bool:
    return bool(state["meta"].get("tool_only_steps", 0) >= limit)


def hold(state: State, limit: int) -> None:
    if exhausted(state, limit) and not state["meta"].get("tool_wake_pause"):
        state["meta"]["tool_wake_pause"] = {
            "reason": "tool_only_budget",
            "steps": state["meta"].get("tool_only_steps", 0),
            "limit": limit,
            "epoch": state["meta"].get("tool_budget_epoch", 0),
        }


def reset(state: State, identity: str) -> None:
    key = "tool_budget:" + identity
    if key in state["controls"]:
        return
    state["controls"][key] = {"applied": True}
    state["meta"].update(
        tool_only_steps=0,
        tool_wake_pause=None,
        tool_budget_epoch=state["meta"].get("tool_budget_epoch", 0) + 1,
    )
