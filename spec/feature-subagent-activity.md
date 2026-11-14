# Delegated execution activity

Status: Implemented

## Overview

Owner-authenticated activity exposes durable work performed by one delegated agent. It does not
rely on the agent remembering to send progress notes. Steps, tool lifecycle and final responses
are projected into bounded chronological entries with trace links. Delegated storage identity is
resolved server-side from the verified owner and agent ID.

## Contract

POST /subagents/activity accepts agent_id and bounded cursor/limit arguments. It returns execution
state and chronological activity entries, plus next_cursor. The same owner can follow a coding
agent or another delegated assignment. Another user's agent is never accessible. Large tool bodies,
provider requests, credentials and private execution environment are excluded from the projection;
full traces remain separately authenticated.

The latest page defaults to 60 entries (maximum 100); `after` retrieves subsequent entries.
Assistant text is bounded to 2,000 characters per entry. Tool bodies, arguments and model reasoning
are omitted. `query_trace` accepts an optional agent_id only after verifying its owner; it cannot
address another user's delegated partition. This endpoint adds no model-facing tool.
