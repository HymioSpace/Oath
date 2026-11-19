from unittest.mock import patch

import pytest

from core.protocol.datatype.tool import ToolInvocation
from core.protocol.datatype.user import User
from core.protocol.error import ToolExecutionError
from core.tasks.fetch import TaskFetchTool


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:secret@example.com",
        "https://example.com:8080",
        "https://127.0.0.1",
    ],
)
def test_research_cannot_access_local_or_credentialed_sources(url):
    with pytest.raises(ToolExecutionError):
        TaskFetchTool().execute(
            ToolInvocation(User("alice", "a" * 32), "fetch", {"url": url}, None)
        )


def test_private_dns_is_rejected_before_http():
    with (
        patch(
            "core.tasks.fetch.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.1", 443))]
        ),
        pytest.raises(ToolExecutionError),
    ):
        TaskFetchTool().execute(
            ToolInvocation(User("alice", "a" * 32), "fetch", {"url": "https://example.com"}, None)
        )
