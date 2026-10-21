"""Read the durable continuation of a tool invocation, without running the agent."""


def execution_status(state, invocation_id):
    call = state["calls"].get(invocation_id)
    if not call:
        raise ValueError("Execution not found")
    origin = state["steps"].get(call["batch_id"])
    if not origin:
        raise ValueError("Execution not found")
    steps = sorted(
        (
            s
            for s in state["steps"].values()
            if s["trace_id"] == origin["trace_id"] and s["trace_started"] >= origin["trace_started"]
        ),
        key=lambda s: s["trace_started"],
    )
    for step in steps:
        response = step.get("response") or {}
        if step["status"] in {"failed", "paused", "cancelled"}:
            return {
                "status": step["status"],
                "content": step.get("error") or "",
                "trace_id": origin["trace_id"],
            }
        if step["status"] == "completed" and response and not response.get("tool_calls"):
            return {
                "status": "completed",
                "content": response.get("content", ""),
                "trace_id": origin["trace_id"],
            }
    return {"status": "running", "content": "", "trace_id": origin["trace_id"]}
