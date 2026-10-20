<p align="center">
  <img src="assets/oath-banner.svg" alt="Oath — Close the chat. The work continues." width="880" />
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-4b70ff?style=flat-square" alt="MIT License" /></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.11%2B-182237?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+" /></a>
  <a href="https://github.com/HymioSpace"><img src="https://img.shields.io/badge/by-HymioSpace-182237?style=flat-square" alt="By HymioSpace" /></a>
</p>

<p align="center">
  <strong>English</strong> · <a href="README.zh-CN.md">简体中文</a><br />
  <a href="#why-oath">Why Oath</a> · <a href="#three-foundations">Foundations</a> · <a href="#quick-start">Quick start</a> · <a href="#bring-your-own-capabilities">Extensions</a> · <a href="#documentation">Docs</a>
</p>

**Oath is an agent runtime for personal agents that have work to do between conversations.**

Give your agent a task, close the client, and come back to its progress. Oath separates communication from execution: accepted work continues on the server, events are persisted, and reconnecting clients receive what they missed.

<p align="center">
  <img src="assets/oath-offline-demo.gif" alt="Illustrated workflow: a user starts a workspace task, disconnects while Oath continues executing tools, then reconnects and receives the saved results." width="880" />
</p>

<p align="center"><sub>Illustrated workflow with synthetic content, not a bundled chat UI.</sub></p>

## Why Oath

A personal agent needs a life beyond an open chat window. It must accept new input while work is underway, wait for a decision without blocking everything else, and preserve progress across disconnects and interruptions.

Oath gives that work a home:

- **Work outlives the connection.** Closing a WebSocket does not cancel accepted work. Reconnect to receive persisted events in order, with explicit offline boundaries and a cursor for each device.
- **Available around the clock, active when needed.** A shared worker pool runs event-triggered steps. No permanent thread per user; an idle Supervisor check makes no model call. Execution still respects step and time budgets.
- **Independent services, one Docker command.** Gateway, intake, scheduling, execution, tools, business APIs and supervision communicate through service contracts. Start the single-host deployment locally or on a cloud Docker host with `oath start`.

## Three foundations

### Event-driven

User messages, tool results and system interventions enter separate queues. Each consumer decides when its inputs should wake the agent. Brain receives a fixed input batch, builds context, calls the model and dispatches the next actions.

**Even a tool result is an event.** Agent progression is driven by incoming work and durable buffers, rather than a single blocking `while True` reasoning loop. With an approval policy, one tool can wait for a user decision while other submitted tools execute. Available results can drive the next step; a later approval or result enters through the event system.

### Durable

An append-only journal records execution facts. Inputs, tool states, execution ownership and outbound events are persisted through the Storage service. Transactional outboxes and fenced leases connect those records to queue delivery and worker recovery.

Recovery uses recorded state. An unconfirmed external side effect is not a promise of exactly-once execution; tools still need their own idempotency where it matters.

### Supervised

The Supervisor is an independent observer of the agent's work, with system events as its path to intervention. Today it periodically checks durable pending work and requests a wake-up; empty buffers never call the model.

That observer role is a foundation to build on. Trace-aware checks, recovery decisions and model-assisted supervision are future directions. Process restarts currently belong to Docker or the deployment platform.

## A shell, small commands, fewer round trips

Oath pairs a core **Bash tool** with **`oath-cli`**, a client for application commands exposed over HTTP. A shell script can compose several commands, pipe and filter their outputs, and return one useful result to the agent. Work that fits a script can take fewer model turns than calling each operation separately.

The execution environment is an interface; the current implementation runs commands in the Tools container. CLI extensions receive the user's current context from Biz, so applications can add their own behavior and authorization.

The core also includes tool deadlines and approvals, skills, tool-result truncation with detail retrieval, context compaction, and traces with raw model requests/responses, causal step links and timing metrics.

## Quick start

Requires Docker with Compose, Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/). Start Docker, then:

```bash
git clone https://github.com/HymioSpace/Oath.git
cd Oath
scripts/oath-install
```

Edit **`~/.oath/oath.toml`**: set the model, its OpenAI-compatible `api_base`, and a Vault `credential` alias. The installer creates local service credentials and reuses an existing config.

```bash
oath start
# First start builds the image and prompts for the model API key if needed.
# Once the services are healthy, open the included debug client:
.venv/bin/python scripts/terminal_client.py
```

The model key is stored in Vault. No `.env` file is needed. Keep the source checkout: the current installation builds its Docker image from it.

| Endpoint | Use |
| --- | --- |
| `ws://127.0.0.1:8765` | Messages, approvals, streaming events and offline replay |
| `http://127.0.0.1:8766` | Trace queries and CLI business APIs |

Both bind to localhost by default. Default access is trusted: supply an external `user_id` (the debug client defaults to `local`). Keep these ports private unless authentication is enabled. One `user_id` has one ongoing conversation, with no session creation.

```bash
oath status
oath restart   # reuses the saved config and extension paths
oath stop      # keeps data volumes
```

Use `--config PATH` for a different TOML. The debug client supports `/status`, `/retry`, `/cancel`, `/approve ID`, `/deny ID` and `/quit`; `--json` shows raw protocol frames.

### Your agent's instructions

```bash
oath start --system-prompt ./prompts/assistant.md
```

This replaces the application instructions with a UTF-8 Markdown file, mounted read-only.
Alternatively set `[agent].system_prompt_file` relative to your TOML and remove the inline
`system_prompt`. Oath appends its actual workspace and skill index; live state stays in later
conversation inputs. Restart after editing the file; frozen requests remain unchanged.

`[agent].max_tool_only_steps = 100` bounds consecutive tool-only steps. At the limit,
results remain buffered until user input or an explicit system intervention arrives.
Periodic Supervisor invokes do not reset it. Existing run step/time limits still apply;
the first limit reached wins. See [the contract](spec/feature-prompt-and-tool-budget.md).

## Bring your own capabilities

Core owns execution semantics and essential tools. Application capabilities live in extensions: **Python tools, HTTP-backed CLI commands and Markdown skills**. Keep them in your own repository and pass their directories at startup:

```bash
oath start \
  --skill /path/to/skills \
  --tool /path/to/tools \
  --cli /path/to/cli
```

Each option can be repeated. Built-in extensions load by default; additional directories are mounted read-only into the services that use them. Duplicate tool or command names fail at startup. See the [extension contracts](spec/feature-oath-command.md).

## Current scope

Oath is early and evolving. The default deployment is single-host Compose with one SQLite owner, one RabbitMQ node and one Vault node. Separate service boundaries support distributed deployments; automatic database failover and broker clustering are not included.

The current Bash environment and default allow-all tool policy are for trusted deployments. Approval execution paths exist; policy configuration and stronger tenant isolation need further work.

## Documentation

- [Module overview](spec/overview.md)
- [Distributed execution and recovery](spec/feature-distributed-personal-agent.md)
- [Client protocol and operations](spec/manual.md)
- [Command and extension contracts](spec/feature-oath-command.md)
- [Vault credential flow](spec/feature-vault.md)

Design documents are currently in Chinese. Start with the overview and distributed-agent spec; older feature specs can describe superseded designs.

## Contributing

Ideas, issues and contributions are welcome. Discuss substantial execution changes first, and document their contracts under `spec/`. Application-specific capabilities belong in extensions.

## License

[MIT](LICENSE). Part of [HymioSpace](https://github.com/HymioSpace). Named after MONO's *Oath*: a commitment to carry work through.

## Application authentication

Oath does not create application accounts. By default, `oath start` trusts a caller-provided identity: WS `agent.attach` supplies `user_id`, and Biz HTTP uses `X-Oath-User-Id`. Keep trusted endpoints on localhost or behind an authenticated gateway.

Enable remote verification through startup arguments:

```bash
oath start --auth-mode remote \
  --auth-url http://hymio-backend:8080/internal/auth/verify \
  --auth-token-file /path/to/private/verifier-service-token
```

The verifier receives a service-authenticated POST with `{"credential": "USER_ACCESS_TOKEN"}` and returns `{"user_id": "external-user-id", "expires_at": 1792300000}`. Rejected, expired and unavailable verification all deny access. Gateway rechecks live WS connections within 30 seconds and at credential expiry; logout does not stop accepted Agent work. Biz verifies each request. Only Gateway/Biz mount the verifier credential; Bash and model context never receive it.

`oath restart` retains the authentication arguments. `--auth-mode static` enables the manually configured `services.users` token table. Auth mode is not read from TOML. The internal CLI still requires its scoped capability in every mode. See [authentication design](spec/feature-authentication.md).

### Model selection

Set `agent.selectable_models` to additional model IDs supported by the configured provider. A `user.message` may include `model`; Oath rejects IDs outside this allowlist plus the default provider model. The selected model is persisted per user and frozen in each step, including retries and tool continuations. Choices in `selectable_models` share the default provider. Configure `agent.model_providers.<choice>` for a separate API endpoint, model ID, Vault credential and parameters; see the commented DeepSeek example in `oath.example.toml`.
