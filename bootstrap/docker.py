"""Single-host Compose bootstrap for Vault and Oath."""

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from bootstrap.common.config import load_config


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(value)


class VaultAdmin:
    def __init__(self, config):
        self.config = config
        self.base = config.secrets.vault_url.rstrip("/")
        self.private = config.data_root / "private"
        self.bootstrap = self.private / "vault-bootstrap.json"
        self.http = httpx.Client(timeout=5, trust_env=False)
        self.root_token = None

    def call(self, method, path, *, payload=None, authorized=False, missing=False):
        headers = {"X-Vault-Token": self.root_token} if authorized else {}
        response = self.http.request(
            method, self.base + "/v1/" + path, json=payload, headers=headers
        )
        if missing and response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json() if response.content else {}

    def start(self):
        deadline = time.monotonic() + 60
        while True:
            try:
                status = self.call("GET", "sys/init")
                break
            except httpx.RequestError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Vault did not start") from None
                time.sleep(0.25)
        if not status["initialized"]:
            if self.bootstrap.exists():
                raise RuntimeError(
                    "Vault data is new, but old bootstrap keys exist; refusing reset"
                )
            initialized = self.call(
                "PUT", "sys/init", payload={"secret_shares": 1, "secret_threshold": 1}
            )
            _save(
                self.bootstrap,
                json.dumps(
                    {
                        "unseal_key": initialized["keys_base64"][0],
                        "root_token": initialized["root_token"],
                    }
                ),
            )
        if not self.bootstrap.exists():
            raise RuntimeError("Vault bootstrap keys missing; restore them before starting")
        saved = json.loads(self.bootstrap.read_text())
        self.root_token = saved["root_token"]
        health = self.call("GET", "sys/health?sealedcode=200&uninitcode=200")
        if health["sealed"]:
            self.call("PUT", "sys/unseal", payload={"key": saved["unseal_key"]})

    def configure(self):
        aliases = {
            provider.credential
            for provider in [
                self.config.agent.provider,
                *self.config.agent.model_providers.values(),
            ]
        }
        if any(not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", alias) for alias in aliases):
            raise ValueError("Provider credentials must be safe Vault aliases")
        mounts = self.call("GET", "sys/mounts", authorized=True)
        if "oath/" not in mounts:
            self.call(
                "POST",
                "sys/mounts/oath",
                payload={"type": "kv", "options": {"version": "2"}},
                authorized=True,
            )
        auth = self.call("GET", "sys/auth", authorized=True)
        if "approle/" not in auth:
            self.call("POST", "sys/auth/approle", payload={"type": "approle"}, authorized=True)
        policy = "\n".join(
            f'path "oath/data/{alias}" {{ capabilities = ["read"] }}' for alias in sorted(aliases)
        )
        self.call(
            "PUT", "sys/policies/acl/oath-worker", payload={"policy": policy}, authorized=True
        )
        self.call(
            "POST",
            "auth/approle/role/oath-worker",
            payload={
                "token_policies": ["oath-worker"],
                "token_ttl": "1h",
                "token_max_ttl": "24h",
                "secret_id_ttl": 0,
            },
            authorized=True,
        )
        role = self.call("GET", "auth/approle/role/oath-worker/role-id", authorized=True)
        secret = self.call("POST", "auth/approle/role/oath-worker/secret-id", authorized=True)
        _save(self.config.secrets.role_id_file, role["data"]["role_id"])
        _save(self.config.secrets.secret_id_file, secret["data"]["secret_id"])

    def seed(self, *, replace=False):
        seen = set()
        for provider in [self.config.agent.provider, *self.config.agent.model_providers.values()]:
            if provider.credential not in seen:
                self._seed_provider(provider, replace=replace)
                seen.add(provider.credential)

    def _seed_provider(self, provider, *, replace=False):
        path = "oath/data/" + quote(provider.credential, safe="/")
        if not replace and self.call("GET", path, authorized=True, missing=True):
            return
        value = os.environ.get(provider.import_env, "") if provider.import_env else ""
        if not value:
            if not sys.stdin.isatty():
                raise RuntimeError(
                    f"Set {provider.import_env} for bootstrap or use an interactive terminal"
                )
            value = getpass.getpass(f"API key for {provider.credential}: ")
        if not value:
            raise ValueError("Empty model API key")
        self.call("POST", path, payload={"data": {"value": value}}, authorized=True)
        print(f"Vault secret ready: {provider.credential}")

    def close(self):
        self.http.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("up", "down", "set-secret", "status", "logs"))
    parser.add_argument("--config", default="oath.toml")
    parser.add_argument("--compose", default="compose.yaml")
    parser.add_argument("--overlay")
    parser.add_argument("--project")
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--volumes", action="store_true")
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--recreate", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    if not config.services.rabbitmq_password or len(config.services.internal_token) < 32:
        raise SystemExit("Run scripts/oath-install first")
    vault_port = urlsplit(config.secrets.vault_url).port or 8200
    env = {
        **os.environ,
        "RABBITMQ_USER": config.services.rabbitmq_user,
        "RABBITMQ_PASSWORD": config.services.rabbitmq_password,
        "OATH_WS_PORT": str(config.services.gateway_port),
        "OATH_HTTP_PORT": str(config.services.biz_port),
        "OATH_VAULT_PORT": str(vault_port),
        "OATH_CONFIG_PATH": str(Path(args.config).resolve()),
        "OATH_ROLE_ID_PATH": str(config.secrets.role_id_file.resolve()),
        "OATH_SECRET_ID_PATH": str(config.secrets.secret_id_file.resolve()),
    }
    cmd = ["docker", "compose", "-f", str(Path(args.compose).resolve())]
    if args.overlay:
        cmd.extend(("-f", str(Path(args.overlay).resolve())))
    if args.project:
        cmd.extend(("-p", args.project))
    if args.action == "down":
        subprocess.run(
            [*cmd, "down", *(["--volumes"] if args.volumes else [])],
            env=env,
            check=True,
        )
        return
    if args.action in {"status", "logs"}:
        subcommand = (
            ["ps"] if args.action == "status" else ["logs", *(["-f"] if args.follow else [])]
        )
        subprocess.run([*cmd, *subcommand], env=env, check=True)
        return
    subprocess.run([*cmd, "up", "-d", "vault"], env=env, check=True)
    admin = VaultAdmin(config)
    try:
        admin.start()
        admin.configure()
        admin.seed(replace=args.action == "set-secret")
    finally:
        admin.close()
    if args.action == "up":
        subprocess.run(
            [
                *cmd,
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "120",
                *(["--no-build", "--pull", "never"] if args.no_build else ["--build"]),
            ],
            env=env,
            check=True,
        )
        if args.recreate:
            subprocess.run(
                [
                    *cmd,
                    "up",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    "storage",
                    "queue",
                    "manager",
                    "tools",
                    "consumer",
                    "worker",
                    "supervisor",
                    "gateway",
                    "biz",
                ],
                env=env,
                check=True,
            )
            subprocess.run(
                [*cmd, "up", "-d", "--wait", "--wait-timeout", "120"], env=env, check=True
            )


if __name__ == "__main__":
    main()
