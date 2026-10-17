"""Prepare a private oath.toml without placing model credentials in it."""

import json
import secrets
import shutil
import tomllib
from pathlib import Path
from urllib.parse import quote

from bootstrap.common.config import load_config


def _toml(value):
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(f"{json.dumps(key)} = {_toml(item)}" for key, item in value.items())
            + " }"
        )
    return json.dumps(value)


def _table(raw, header, additions):
    lines = raw.splitlines(keepends=True)
    marker = f"[{header}]\n"
    if marker not in lines:
        return (
            raw.rstrip()
            + "\n\n"
            + marker
            + "".join(f"{name} = {_toml(value)}\n" for name, value in additions.items())
        )
    start = lines.index(marker) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("[")), len(lines))
    existing = tomllib.loads(raw)
    table = existing
    for name in header.split("."):
        table = table[name]
    for name, value in additions.items():
        if name in table and table[name] in ("", {}):
            for index in range(start, end):
                if lines[index].startswith(name + " ="):
                    lines[index] = f"{name} = {_toml(value)}\n"
                    break
    new_lines = [
        f"{name} = {_toml(value)}\n" for name, value in additions.items() if name not in table
    ]
    return "".join(lines[:end] + new_lines + lines[end:])


def main():
    root = Path(__file__).resolve().parents[1]
    config = Path.home() / ".oath" / "oath.toml"
    config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.parent.chmod(0o700)
    legacy_config = root / "oath.toml"
    migrating = not config.exists() and legacy_config.exists()
    if not config.exists():
        shutil.copyfile(legacy_config if migrating else root / "oath.example.toml", config)
        config.chmod(0o600)
    if migrating:
        source_private = load_config(legacy_config).data_root / "private"
        target_private = load_config(config).data_root / "private"
        if source_private.is_dir() and source_private.resolve() != target_private.resolve():
            if target_private.exists():
                raise SystemExit(f"Refusing to overwrite existing private data: {target_private}")
            shutil.copytree(source_private, target_private)
            target_private.chmod(0o700)
    raw = config.read_text()
    data = tomllib.loads(raw)
    legacy = root / ".env"
    legacy_values = {}
    if legacy.exists():
        legacy_values = dict(
            line.split("=", 1)
            for line in legacy.read_text().splitlines()
            if "=" in line and not line.startswith("#")
        )
    if "api_key_env" in data.get("agent", {}).get("provider", {}):
        old = data["agent"]["provider"]["api_key_env"]
        raw = (
            "\n".join(
                line for line in raw.splitlines() if not line.lstrip().startswith("api_key_env =")
            )
            + "\n"
        )
        raw = _table(raw, "agent.provider", {"credential": "model/main", "import_env": old})
    users = json.loads(legacy_values.get("OATH_USERS", "{}")) if legacy_values else {}
    if not users:
        users = {secrets.token_urlsafe(32): "local"}
    password = legacy_values.get("RABBITMQ_PASSWORD") or secrets.token_hex(24)
    broker_user = legacy_values.get("RABBITMQ_USER") or "oath"
    raw = _table(
        raw,
        "services",
        {
            "internal_token": legacy_values.get("OATH_SERVICE_TOKEN") or secrets.token_urlsafe(32),
            "users": users,
            "rabbitmq_user": broker_user,
            "rabbitmq_password": password,
            "broker_url": (
                f"amqp://{quote(broker_user, safe='')}:{quote(password, safe='')}"
                "@127.0.0.1:5672/%2F"
            ),
        },
    )
    raw = _table(
        raw,
        "secrets",
        {
            "vault_url": "http://127.0.0.1:8200",
            "role_id_file": "./data/private/vault-role-id",
            "secret_id_file": "./data/private/vault-secret-id",
        },
    )
    tomllib.loads(raw)
    if raw != config.read_text():
        backup = config.with_name("oath.toml.pre-vault.local")
        if backup.exists():
            raise SystemExit(f"Refusing to overwrite {backup}")
        shutil.copyfile(config, backup)
        config.write_text(raw)
        config.chmod(0o600)
    selected = load_config(config)
    private = selected.data_root / "private"
    private.mkdir(parents=True, exist_ok=True)
    private.chmod(0o700)
    selected.agent.workspace_root.mkdir(parents=True, exist_ok=True)
    if legacy.exists():
        archived = private / "legacy-environment.backup"
        if archived.exists():
            raise SystemExit(f"Move {legacy} manually; {archived} already exists")
        legacy.rename(archived)
        archived.chmod(0o600)
        print(f"Old .env archived to {archived}; it is no longer loaded")
    config.chmod(0o600)
    print(f"Ready. Edit {config}, then run oath start")


if __name__ == "__main__":
    main()
