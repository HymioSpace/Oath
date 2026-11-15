"""Read-only public HTTPS input for isolated research tasks."""

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

import httpx

from core.protocol.datatype.tool import ToolDefinition, ToolResult
from core.protocol.error import ToolExecutionError
from core.protocol.interface.tool import Tool


class TaskFetchTool(Tool):
    allow_subagent = True
    subagent_only = True

    @property
    def definition(self):
        return ToolDefinition(
            "task_fetch",
            (
                "Read a public HTTPS source for the current research task. "
                "Returns source URL and a bounded text excerpt. No login or "
                "write actions."
            ),
            {
                "type": "object",
                "properties": {"url": {"type": "string", "maxLength": 2048, "minLength": 9}},
                "required": ["url"],
                "additionalProperties": False,
            },
        )

    def execute(self, invocation):
        url = invocation.arguments["url"]
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.port not in {None, 443}
        ):
            raise ToolExecutionError("Only public HTTPS URLs without credentials are supported")
        try:
            addresses = socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
            if not addresses or any(
                not ipaddress.ip_address(row[4][0]).is_global for row in addresses
            ):
                raise ToolExecutionError("Source must resolve to public addresses")
            # Pin the validated address: a second DNS lookup must not reach a private host.
            address = addresses[0][4][0]
            host = "[" + address + "]" if ":" in address else address
            pinned = urlunsplit((parts.scheme, host, parts.path, parts.query, ""))
            with (
                httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client,
                client.stream(
                    "GET",
                    pinned,
                    headers={
                        "Host": parts.hostname,
                        "Accept": "text/plain, text/html, application/json",
                    },
                    extensions={"sni_hostname": parts.hostname},
                ) as response,
            ):
                if response.is_redirect:
                    return ToolResult(
                        {
                            "url": url,
                            "status": response.status_code,
                            "redirect": response.headers.get("location", ""),
                        }
                    )
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk[: 262144 - len(content)])
                    if len(content) >= 262144:
                        break
                return ToolResult(
                    {
                        "url": url,
                        "status": response.status_code,
                        "content": content.decode("utf-8", errors="replace")[:30000],
                        "truncated": len(content) >= 30000,
                    }
                )
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise ToolExecutionError(
                "Source could not be read; check the URL or use another source"
            ) from exc
