# Bounded state transitions over RPC

Status: Implemented

Runtime callbacks retain their complete state view, while bundled service clients send only
changed table entries through `state.patch`. Storage merges entries under the same transaction,
checks the expected version and lease, archives completed requests, and performs the existing
journal/outbox projections atomically. Entries cannot be deleted through this operation.

Patch receipts fingerprint the original patch, including its expected version and reason.
Repeating a committed identity succeeds even after subsequent commits; changing its payload
is rejected. Conflicts require a fresh snapshot and callback evaluation.

The response contains the committed version and newly archived request identities, so the
worker's reusable snapshot matches Storage's request compaction. The original `state.commit`
operation remains available. Bundled bootstrap enables patches only with the matching Storage
service release; third-party StoragePort implementations keep full-state commits by default.

This changes an internal service operation, not application events, persisted state schema,
user identity, or queue ordering. Runtime mutation, durability, fencing and recovery semantics
remain the same.

`state.entries` returns bounded selections from one state table. Cancellation checks, tool
classification and worker completion use this read path instead of transferring unrelated
conversation history. Tool scheduling uses the transaction's returned eligibility snapshot;
the execution transition still checks cancellation, deadlines and status against current state.
