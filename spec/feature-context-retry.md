# Context dispatch retry

Status: Implemented.

A repeated queue identity compares business input (kind/source/source_id/payload), excluding
receipt time and causal telemetry. Gateway assigns a new trace on each receipt; that trace
must not turn an identical durable retry into a conflicting command. The first accepted input
and its telemetry remain authoritative; changed business payload is still rejected.
