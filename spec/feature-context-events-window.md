# Event context and window policy

Status: Implemented

## Overview

Input ownership remains Intake's responsibility. After a wake, one step claims fixed inputs,
renders non-tool events as escaped XML, evaluates the context budget and freezes one request.
Retries reuse that request. Tools retain native call/result pairing.

```text
command / application event → queue → consumer buffer → step.input
→ event XML + full conversation → budget / optional compaction → step.request → provider
```

## Event view

Each non-tool input is an `event` with `type` and stable `id`, a `content` child and a
`dynamic` child. `dynamic.time` is the input's persisted occurrence time, never a new clock
read when replaying. Missing occurrence time uses the persisted buffer time.
Adjacent events claimed by the same step share one user message, inside `events`.
User IDs retain the `user:` reference prefix used by application evidence tools.
XML values are escaped. A system-queue event remains data in the user role, not privileged
system instructions. The runtime prompt explains event, dynamic and summary semantics.

Optional `payload.dynamic` carries event-specific JSON under `dynamic.context`. It is
bounded to 4 KiB at user ingress. A producer may include location or timezone when available;
the runtime does not collect location or expose the private `user_context` map or telemetry.
Tool results and late tool-result notifications retain their existing representation.

## Window policy

User choices are 128,000 / 256,000 / 512,000 / 1,000,000 tokens. Default: 256,000.
The effective window is the lesser of the selected size and the configured model capacity.
The runtime and application catalogs declare the same default capacities; deployments may
override them within each model's supported maximum. Unknown models use a conservative 128,000 until configured.
Input plus system prompt, tool schemas, preserved reasoning and reserved output count toward
the budget. Per-step selection and capacity are frozen outside provider request parameters.

40% asynchronously prepares a durable pending summary; 90% first applies pending and then,
if necessary, synchronously compresses once. Both ratios remain configurable. Background work
never changes an in-flight request or waits on a lease held by another compaction. A request
below the hard window proceeds while asynchronous preparation runs; an in-flight asynchronous
job never occupies an Agent Worker waiting for the same user lease. Raw history is retained; summary application changes only
the model view. An oversized retained tail causes an explicit bounded-input error rather than
an oversized provider call or silent deletion of recent inputs.

Compression uses the configured compression model (the default provider), whose own input
limit also applies. Large transcripts are summarized in bounded chunks, carrying the previous
digest into each subsequent chunk. Invalid or over-budget summaries are rejected.

## Cross-repository contract

Hymio stores the selected window alongside model preferences, forwards it on `user.message`,
and exposes selected/effective sizes in Web and iOS settings. `context_window` is execution
configuration, not model-visible event content or a vendor request parameter.
Nostalgia's durable personal-context protocol is unchanged.
