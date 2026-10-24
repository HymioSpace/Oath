"""Low-cardinality operational metrics exported in Prometheus text format."""

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    ProcessCollector,
    generate_latest,
    start_http_server,
)

REGISTRY = CollectorRegistry()
ProcessCollector(registry=REGISTRY)
LATENCY = (0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)
SIZE = (128, 512, 2048, 8192, 32768, 131072, 524288, 2097152, 8388608, 33554432)

STORAGE_LOCK_WAIT = Histogram(
    "oath_storage_lock_wait_seconds",
    "Time acquiring a SQLite transaction.",
    ("mode",),
    buckets=LATENCY,
    registry=REGISTRY,
)
STORAGE_TRANSACTIONS = Histogram(
    "oath_storage_transaction_seconds",
    "SQLite transaction duration including lock wait.",
    ("mode", "outcome"),
    buckets=LATENCY,
    registry=REGISTRY,
)
STORAGE_BYTES = Gauge(
    "oath_storage_bytes",
    "Storage bytes by representation.",
    ("kind",),
    registry=REGISTRY,
)

RPC_REQUESTS = Counter(
    "oath_rpc_requests_total",
    "Internal RPC requests by service, operation and HTTP status.",
    ("service", "operation", "status"),
    registry=REGISTRY,
)
RPC_DURATION = Histogram(
    "oath_rpc_duration_seconds",
    "Internal RPC handling latency.",
    ("service", "operation", "status"),
    buckets=LATENCY,
    registry=REGISTRY,
)
RPC_CLIENT_DURATION = Histogram(
    "oath_rpc_client_duration_seconds",
    "Caller-observed internal RPC latency.",
    ("target", "operation", "status"),
    buckets=LATENCY,
    registry=REGISTRY,
)
RPC_PAYLOAD_BYTES = Histogram(
    "oath_rpc_request_bytes",
    "Internal RPC request body size.",
    ("service", "operation"),
    buckets=SIZE,
    registry=REGISTRY,
)
RPC_INFLIGHT = Gauge(
    "oath_rpc_inflight",
    "Internal RPC requests currently executing.",
    ("service",),
    registry=REGISTRY,
)
GATEWAY_CONNECTIONS = Gauge(
    "oath_gateway_connections",
    "Authenticated WebSocket connections.",
    registry=REGISTRY,
)
GATEWAY_COMMANDS = Counter(
    "oath_gateway_commands_total",
    "Gateway commands by type, channel and result.",
    ("type", "channel", "status"),
    registry=REGISTRY,
)
GATEWAY_DURATION = Histogram(
    "oath_gateway_command_duration_seconds",
    "Gateway command handling latency.",
    ("type", "channel", "status"),
    buckets=LATENCY,
    registry=REGISTRY,
)

QUEUE_PUBLISH_DELAY = Histogram(
    "oath_queue_publish_delay_seconds",
    "Time from durable queue acceptance to broker-confirmed publish.",
    ("kind",),
    buckets=LATENCY,
    registry=REGISTRY,
)
QUEUE_TO_BUFFER = Histogram(
    "oath_queue_to_buffer_seconds",
    "Time from gateway acceptance to consumer buffer insertion.",
    ("kind",),
    buckets=LATENCY,
    registry=REGISTRY,
)
BUFFER_WAKE_DELAY = Histogram(
    "oath_buffer_wake_delay_seconds",
    "Time from inbound arrival to agent claim.",
    ("kind",),
    buckets=LATENCY,
    registry=REGISTRY,
)
BRAIN_CLAIM_WAIT = Histogram(
    "oath_brain_claim_wait_seconds",
    "Time a ready user waits before a worker claims the brain lease.",
    buckets=LATENCY,
    registry=REGISTRY,
)

STEP_DURATION = Histogram(
    "oath_step_duration_seconds",
    "Agent step duration.",
    ("channel", "status"),
    buckets=LATENCY,
    registry=REGISTRY,
)
STEP_PAUSED = Counter(
    "oath_step_paused_total",
    "Steps paused before model execution.",
    ("reason",),
    registry=REGISTRY,
)
COMPACTION_ATTEMPTS = Counter(
    "oath_compaction_attempts_total",
    "Context compaction attempts by mode and terminal outcome.",
    ("mode", "outcome", "reason"),
    registry=REGISTRY,
)
COMPACTION_DURATION = Histogram(
    "oath_compaction_duration_seconds",
    "Context compaction duration including the model call.",
    ("mode", "outcome"),
    buckets=LATENCY,
    registry=REGISTRY,
)
COMPACTION_LEASE_WAIT = Histogram(
    "oath_compaction_lease_wait_seconds",
    "Time spent acquiring the per-user compaction lease.",
    ("mode",),
    buckets=LATENCY,
    registry=REGISTRY,
)
COMPACTION_BLOCKED_STEPS = Counter(
    "oath_compaction_blocked_steps_total",
    "Steps that could not proceed because context preparation was incomplete.",
    ("mode",),
    registry=REGISTRY,
)
LLM_DURATION = Histogram(
    "oath_llm_duration_seconds",
    "Model call duration including streaming.",
    ("model", "status"),
    buckets=LATENCY,
    registry=REGISTRY,
)
LLM_TTFT = Histogram(
    "oath_llm_ttft_seconds",
    "Time from model request to first streamed increment.",
    ("model", "channel"),
    buckets=LATENCY,
    registry=REGISTRY,
)
LLM_TOKENS = Counter(
    "oath_llm_tokens_total",
    "Provider-reported model tokens.",
    ("model", "direction", "cache"),
    registry=REGISTRY,
)
TOOL_DURATION = Histogram(
    "oath_tool_duration_seconds",
    "Tool execution duration.",
    ("tool", "outcome"),
    buckets=LATENCY,
    registry=REGISTRY,
)
TOOL_TERMINALS = Counter(
    "oath_tool_terminals_total",
    "Tool terminal outcomes.",
    ("tool", "outcome"),
    registry=REGISTRY,
)
DELIVERY_DELAY = Histogram(
    "oath_delivery_delay_seconds",
    "Time from persisted event creation to WebSocket send.",
    ("type",),
    buckets=LATENCY,
    registry=REGISTRY,
)
TRACE_API_DURATION = Histogram(
    "oath_trace_api_duration_seconds",
    "Trace read latency.",
    ("operation", "status"),
    buckets=LATENCY,
    registry=REGISTRY,
)


def metrics_response() -> tuple[bytes, str]:
    """Return the registry in Prometheus text format."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def start_metrics_server(port: int = 8780) -> None:
    """Expose this process's registry for Prometheus pull scraping."""
    start_http_server(port, addr="0.0.0.0", registry=REGISTRY)


__all__ = [
    "BRAIN_CLAIM_WAIT",
    "BUFFER_WAKE_DELAY",
    "COMPACTION_ATTEMPTS",
    "COMPACTION_BLOCKED_STEPS",
    "COMPACTION_DURATION",
    "COMPACTION_LEASE_WAIT",
    "CONTENT_TYPE_LATEST",
    "DELIVERY_DELAY",
    "GATEWAY_COMMANDS",
    "GATEWAY_CONNECTIONS",
    "GATEWAY_DURATION",
    "LLM_DURATION",
    "LLM_TOKENS",
    "LLM_TTFT",
    "QUEUE_PUBLISH_DELAY",
    "QUEUE_TO_BUFFER",
    "REGISTRY",
    "RPC_DURATION",
    "RPC_CLIENT_DURATION",
    "RPC_INFLIGHT",
    "RPC_PAYLOAD_BYTES",
    "RPC_REQUESTS",
    "STEP_DURATION",
    "STEP_PAUSED",
    "TOOL_DURATION",
    "TOOL_TERMINALS",
    "TRACE_API_DURATION",
    "metrics_response",
    "start_metrics_server",
]
