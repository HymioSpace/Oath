"""Worker-side Vault KV v2 reader authenticated with a mounted AppRole identity."""

import re
from pathlib import Path
from urllib.parse import quote

import httpx


class VaultSecrets:
    def __init__(self, url: str, role_id_file: Path, secret_id_file: Path):
        self.url = url.rstrip("/")
        self.role_id_file = role_id_file
        self.secret_id_file = secret_id_file
        self._token = None
        self._client = httpx.Client(timeout=5, trust_env=False)

    def get(self, alias: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", alias):
            raise ValueError("Invalid secret alias")
        for attempt in range(2):
            if not self._token:
                self._login()
            response = self._client.get(
                f"{self.url}/v1/oath/data/{quote(alias, safe='/')}",
                headers={"X-Vault-Token": self._token},
            )
            if response.status_code == 403 and attempt == 0:
                self._token = None
                continue
            response.raise_for_status()
            value = response.json()["data"]["data"]["value"]
            if not isinstance(value, str) or not value:
                raise ValueError("Vault secret has no value")
            return value
        raise RuntimeError("Vault authentication failed")

    def _login(self):
        response = self._client.post(
            f"{self.url}/v1/auth/approle/login",
            json={
                "role_id": self.role_id_file.read_text().strip(),
                "secret_id": self.secret_id_file.read_text().strip(),
            },
        )
        response.raise_for_status()
        self._token = response.json()["auth"]["client_token"]

    def close(self):
        self._client.close()
