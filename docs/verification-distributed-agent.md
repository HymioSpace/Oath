# Distributed agent verification

Verified on 2026-10-17 with real RabbitMQ, SQLite and independent service processes.
The Docker run also uses a persistent HashiCorp Vault instance and AppRole identity.
Model requests use a deterministic local HTTP endpoint with streaming and tool calls.
No paid provider or production credentials are used.

## Executed checks

| Check | Result |
| --- | --- |
| Ruff: core, bootstrap, biz, scripts, extensions | Passed |
| Python compileall | Passed |
| Shell syntax and terminal client help | Passed |
| Native service end-to-end script | Passed |
| Docker image build and isolated Compose end-to-end script | Passed |

Native scenarios cover offline execution, input arriving during an active step,
replay boundaries, ACK cursors, independent devices, input deduplication,
Bash tool round trip, user isolation, Biz trace queries, Worker termination and
lease recovery, Storage restart, rejection of stale lease writes, RabbitMQ outage
and recovery, context compaction across steps, and idle Supervisor without model calls.

The Docker check mounts a temporary oath.toml, initializes and unseals Vault,
imports a stub model key, creates a Worker AppRole, then creates isolated volumes and ports and starts all services,
executes a Bash tool round trip while the client is disconnected, queries traces,
and reconnects to verify ordered replay. It also verifies Tools has neither the
model key in its environment nor a mounted Vault identity, and Vault rejects
unauthenticated reads. It removes its own containers and volumes.

## Reproduce

```bash
# Use a disposable broker: this script declares Oath queues.
.venv/bin/python scripts/e2e_services.py --amqp-url 'amqp://user:password@localhost:5672/%2F'
# Add --broker-container NAME to test stopping and restarting that disposable broker.

docker build -t oath-runtime:local .
.venv/bin/python scripts/e2e_docker.py
```

The Docker script uses host.docker.internal to reach its model stub; it was verified
with Docker Desktop on macOS. Each script prints its artifact directory on exit.

## Limits

Existing unit tests were not changed or run; they still target the previous protocol.
These checks do not establish multi-machine HA, throughput limits, broker-cluster
failover, every possible publish/ACK crash window, or arbitrary external-tool
exactly-once effects. The default host Bash environment is for trusted users,
not an isolation boundary between untrusted tenants.
