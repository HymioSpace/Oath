import json
from types import SimpleNamespace

from bootstrap.clients.context_provider import provider


def test_provider_sends_authenticated_user_and_bounds_response(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("synthetic-context-provider-token")
    config = SimpleNamespace(
        context_provider_url="http://fixture.invalid/context",
        context_provider_token_file=str(token),
    )
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            assert limit == 65537
            return json.dumps({"content": "Specific user understanding"}).encode()

    def open_request(request, timeout):
        seen.append(json.loads(request.data))
        assert timeout == 8
        assert request.headers["Authorization"] == "Bearer synthetic-context-provider-token"
        return Response()

    monkeypatch.setattr("bootstrap.clients.context_provider.urlopen", open_request)
    assert (
        provider(config)(SimpleNamespace(user_id="user-a", agent_id=""), "ios")
        == "Specific user understanding"
    )
    assert seen == [{"user_id": "user-a", "agent_id": "", "channel": "ios"}]


def test_provider_failure_is_explicit(tmp_path):
    config = SimpleNamespace(
        context_provider_url="http://fixture.invalid/context",
        context_provider_token_file=str(tmp_path / "missing"),
    )
    assert "unavailable" in provider(config)(SimpleNamespace(user_id="user-a"))
