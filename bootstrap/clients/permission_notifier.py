"""Application callback used to surface durable approval requests."""

from __future__ import annotations

import time
from typing import Any

import httpx


class PermissionNotifier:
    def __init__(self, url: str, token: str, *, timeout: float = 5.0) -> None:
        self.url = url
        self.headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        }
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    def notify(self, user_id: str, approval: dict[str, Any], invocation_id: str) -> None:
        title = str(approval.get("title") or "有一项操作等待确认").strip()[:180]
        summary = str(approval.get("summary") or "").strip()[:180]
        payload = {
            "user_id": user_id,
            "kind": "attention",
            "summary": summary or title,
            "route": "chat",
            "request_id": "approval:" + invocation_id,
        }
        for attempt in range(3):
            try:
                response = self.client.post(self.url, headers=self.headers, json=payload)
                if response.status_code >= 500 and attempt < 2:
                    time.sleep(0.1 * (attempt + 1))
                    continue
                response.raise_for_status()
                return
            except (httpx.TransportError, httpx.TimeoutException):
                if attempt == 2:
                    raise
                time.sleep(0.1 * (attempt + 1))

    def close(self) -> None:
        self.client.close()
