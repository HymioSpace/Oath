# Runtime latency and durable tool results

Status: Implemented

## Overview

A new user input starts a new intent; it does not erase completed work. Every verified tool
terminal remains eligible for context delivery, including results from earlier runs. Claims
remain idempotent and late results retain their original invocation, batch and run identities.

`tool terminal → durable queue → verified buffer → claimed input → frozen model request`

Tool authenticity still requires an exact match with the stored terminal for the same user.
Late terminals close their original tool slot when possible, otherwise appear as attributed
result events. They are facts about previous work, not authorization to repeat an action.

## Execution state and reads

Historical inactive request drafts live in checksummed disk archives. Their step metadata and
recovery identity remain available; active drafts remain hot. Explicit historical reads can
hydrate archives. Resuming work restores the original draft rather than reconstructing it.

Tool service reads exclude unrelated conversation/input history and expose only trace metadata
for steps. Such projected transactions may change tool, batch, permission-control and metadata
entries, never overwrite a partially projected step. Version checks, leases and atomic Journal /
Outbox projection retain their existing semantics. Context budgeting reads only conversation and
compaction state; trace closure reads only the inputs and source steps it needs.

## Observation and recovery

`invoke_agent` measures one Brain step, including model preparation, persistence and tool batch
submission. `chat` measures a provider call; TTFT is its first delta, not the user's end-to-end
wait. Queue-to-claim latency precedes the step. A trace may contain multiple sequential steps and
asynchronous tools; adding all durations double counts nested or overlapping work.

Brain stage events split context lookup, compaction preparation, request freezing, response
persistence, tool submission and final persistence. They describe elapsed time, not CPU usage.
Storage RPC and lock metrics explain contention separately.

An internal bounded result replay operation republishes only an existing, immutable terminal
that has no claimed input or pending inbox entry. It never executes a tool again and cannot
supply replacement result content. Normal context claims deduplicate redelivery.

## Internal recovery RPC

Use authenticated Storage RPC `tools.replay_results` with `user_id` and a list of at most 20
`invocation_ids`. The default `dry_run=true` reports eligibility without writing. Explicit
`dry_run=false` republishes the original terminal through the durable outbox. Already claimed,
buffered or unfinished results are skipped. This operation is intentionally not a public API.

Validation includes consumer supersession, archive restoration, projected-write conflicts, exact
entry selection and replay idempotency. No model invocation is needed for these regressions.
