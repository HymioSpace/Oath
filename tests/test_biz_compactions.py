from starlette.testclient import TestClient

from bootstrap.biz.server import build_app


class _Service:
    def __init__(self):
        self.since = ""

    def list_commands(self):
        return ()

    def compaction_events(self, *, since):
        self.since = since
        return [
            {
                "user_id": "u1",
                "event_type": "context.compaction",
                "payload": {"compression_id": "c1", "status": "started"},
                "occurred_at": "2026-10-24T12:00:00.000000Z",
            }
        ]


def test_admin_compaction_query_requires_its_service_token():
    service = _Service()
    client = TestClient(build_app(service, service, admin_token="a" * 32))

    assert client.get("/admin/compactions").status_code == 401
    response = client.get(
        "/admin/compactions?hours=1",
        headers={"Authorization": "Bearer " + "a" * 32},
    )

    assert response.status_code == 200
    assert response.json()["events"][0]["payload"]["compression_id"] == "c1"
    assert service.since.endswith("Z")
