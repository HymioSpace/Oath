"""有界的服务调用；事务冲突与租约失效保持独立语义。"""

import httpx

from core.protocol.interface.storage import LeaseLost, StorageConflict


class ServiceClient:
    def __init__(self, url, token, timeout=10):
        self.url = url.rstrip("/")
        self.http = httpx.Client(
            timeout=timeout, headers={"Authorization": f"Bearer {token}"}, trust_env=False
        )

    def call(self, operation, **params):
        response = self.http.post(f"{self.url}/v1/{operation}", json=params)
        if response.status_code == 409:
            raise StorageConflict(response.json().get("error", "conflict"))
        if response.status_code == 412:
            raise LeaseLost(response.json().get("error", "lease_lost"))
        if response.status_code == 400:
            raise ValueError(response.json().get("error", "bad_request"))
        response.raise_for_status()
        return response.json()["result"]

    def close(self):
        self.http.close()
