# Model selection

Status: Implemented

## Overview

A user message may select a model from `agent.selectable_models`, `agent.model_providers` keys and the default provider model. Choices in `selectable_models` use the default endpoint and credential. `agent.model_providers.<choice>` defines an independent model ID, API endpoint, Vault credential alias, timeout and provider parameters. Worker routes the frozen choice through this registry; unknown choices fail without calling a fallback endpoint. Oath never accepts an arbitrary endpoint or credential from a client.

```mermaid
flowchart LR
  Message[User message + model] --> Intake --> Frozen[Persisted step request] --> Provider
```

Selection is persisted per user when Intake claims input. Tool continuations keep that choice; a later user selection affects future steps only. Prepared requests retain their model across retry and recovery. Removed choices fall back to the configured default. Compaction uses its configured model.

Hymio owns the authenticated settings UI and injects the saved choice into user messages. Oath independently rejects unavailable model IDs at Gateway; Intake only applies allowed choices.
