# CLI execution through ToolSystem

Status: Implemented

## Overview

Applications select a small native tool surface while keeping registered capabilities callable
through oath-cli. CLI execution uses the same admission, permissions, deadlines, immutable
terminals and traces as native calls. Status is read from ToolSystem, never inferred from a
device command or a later Agent reply.

A Bash invocation receives a short-lived CLI capability bound to its user, channel and parent
invocation. The CLI server is cloud hosted; devices install only the verified client. Nested
invocations retain their parent and original run. Inline CLI results are returned to the shell
without waking the model for every command; explicitly asynchronous calls retain normal terminal
events. Approval remains visible and is never converted into success.

The CLI catalog and execution admission apply delegated-agent and channel restrictions. Native
exposure does not grant CLI permissions. Expired capabilities, unknown parents and cross-user
requests cannot execute. Child submission is idempotent under a client request identifier.

The installer uses HTTPS outside loopback, verifies a pinned SHA-256 and installs atomically in
a user-owned executable directory. It contains no deployment address or credential. Updates use
the same process. CLI credentials are per-execution environment data, not installation content.
