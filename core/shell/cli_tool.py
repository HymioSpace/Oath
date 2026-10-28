"""Execute the installed CLI client without granting a server-side shell."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from core.protocol.datatype import ToolDefinition, ToolResult
from core.protocol.error import ToolExecutionError
from core.protocol.interface import Tool


class CliTool(Tool):
    def __init__(self, client, server_url=""):
        self.server_url = server_url
        self.client = Path(client)

    @property
    def definition(self):
        return ToolDefinition(
            "cli",
            "Call oath-cli on the cloud without a device or shell. "
            "Use help to discover commands and status ID to read ToolSystem state. "
            "Use device Bash for composing multiple commands.",
            {
                "type": "object",
                "properties": {"command": {"type": "string", "maxLength": 8192}},
                "required": ["command"],
                "additionalProperties": False,
            },
        )

    def execute(self, invocation):
        args = shlex.split(invocation.arguments["command"])
        if args and args[0] == "oath-cli":
            args = args[1:]
        with tempfile.TemporaryFile() as output:
            try:
                result = subprocess.run(
                    [sys.executable, str(self.client), *args],
                    env={
                        **os.environ,
                        **invocation.cli_environment,
                        **(
                            {"OATH_CLI_SERVER": self.server_url, "OATH_CLI_INTERNAL_HTTP": "1"}
                            if self.server_url
                            else {}
                        ),
                    },
                    stdout=output,
                    stderr=output,
                    timeout=20,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ToolExecutionError(
                    "CLI still pending; query the original ToolSystem invocation"
                ) from exc
            output.seek(0)
            text = output.read(65536).decode(errors="replace")
        try:
            content = json.loads(text)
        except ValueError:
            content = {"exit_code": result.returncode, "output": text}
        pending = isinstance(content, dict) and content.get("status") in {
            "waiting_approval",
            "pending",
        }
        if result.returncode != 0 and not pending:
            detail = text.strip() or f"exit status {result.returncode}"
            raise ToolExecutionError(f"CLI failed: {detail[:480]}")
        return ToolResult(content)
