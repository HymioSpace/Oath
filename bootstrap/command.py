"""Host command for a single local Oath Compose deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx
import yaml

from bootstrap.common.authentication import auth_arguments, auth_environment
from bootstrap.common.config import load_config

_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE = _ROOT / "compose.yaml"
_KINDS = ("skill", "tool", "cli")
_DEFAULT_CONFIG = Path.home() / ".oath" / "oath.toml"


def _state_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "oath"


def _state_path(name: str) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
        raise ValueError("Deployment name must use lowercase letters, digits and hyphens")
    return _state_dir() / f"{name}.json"


def _validate_directory(raw: str, kind: str) -> str:
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"--{kind} directory does not exist: {path}")
    return str(path)


def _paths(args: argparse.Namespace) -> dict[str, list[str]]:
    return {
        kind: list(dict.fromkeys(_validate_directory(path, kind) for path in getattr(args, kind)))
        for kind in _KINDS
    }


def _overlay(paths: dict[str, list[str]], prompt_file: Path | None = None) -> dict:
    services: dict[str, dict] = {}
    targets = {"skill": ("worker", "tools"), "tool": ("tools",), "cli": ("biz",)}
    for kind, hosts in paths.items():
        containers = [f"/app/oath-external/{kind}/{index}" for index in range(len(hosts))]
        for service in targets[kind]:
            target = services.setdefault(service, {"volumes": [], "environment": {}})
            if kind == "tool":
                target["extra_hosts"] = ["host.docker.internal:host-gateway"]
            target["volumes"].extend(
                {"type": "bind", "source": host, "target": container, "read_only": True}
                for host, container in zip(hosts, containers, strict=True)
            )
            target["environment"][f"OATH_{kind.upper()}_DIRS"] = json.dumps(containers)
    if prompt_file:
        for service in (
            "storage",
            "queue",
            "manager",
            "tools",
            "consumer",
            "worker",
            "supervisor",
            "gateway",
            "biz",
        ):
            target = services.setdefault(service, {"volumes": [], "environment": {}})
            target["volumes"].append(
                {
                    "type": "bind",
                    "source": str(prompt_file),
                    "target": "/run/oath/system-prompt.md",
                    "read_only": True,
                }
            )
            target["environment"]["OATH_SYSTEM_PROMPT_FILE"] = "/run/oath/system-prompt.md"
    return {"services": services}


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content)
    temporary.chmod(0o600)
    temporary.replace(path)


def _save(record: dict) -> None:
    _write_private(_state_path(record["name"]), json.dumps(record, indent=2) + "\n")


def _load(name: str) -> dict:
    path = _state_path(name)
    if not path.is_file():
        raise ValueError("No Oath deployment found; run oath start --config PATH first")
    record = json.loads(path.read_text())
    if record.get("version") != 1:
        raise ValueError("Unsupported Oath deployment record")
    return record


def _metrics_target(raw: Path | None) -> str:
    if raw is None:
        return ""
    path = raw.expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Hymio metrics target file does not exist: {path}")
    return str(path)


def _docker_environment(record: dict) -> dict[str, str]:
    environment = os.environ.copy()
    metrics_target = record.get("hymio_metrics_targets")
    if metrics_target:
        environment["OATH_HYMIO_METRICS_TARGETS"] = metrics_target
    return environment


def _run_docker(action: str, record: dict, *, recreate: bool = False) -> None:
    cmd = [
        sys.executable,
        "-m",
        "bootstrap.docker",
        action,
        "--config",
        record["config"],
        "--compose",
        record["compose"],
        "--overlay",
        record["overlay"],
        "--project",
        record["name"],
    ]
    if recreate:
        cmd.append("--recreate")
    subprocess.run(cmd, check=True, env=_docker_environment(record))


def _endpoints(record: dict) -> tuple[str, str]:
    config = load_config(
        record["config"],
        system_prompt_file=Path(record["system_prompt_file"])
        if record.get("system_prompt_file")
        else None,
    )
    return (
        f"ws://127.0.0.1:{config.services.gateway_port}",
        f"http://127.0.0.1:{config.services.biz_port}",
    )


def _healthy(record: dict) -> None:
    ws, biz = _endpoints(record)
    urls = (ws.replace("ws://", "http://") + "/heartbeat", biz + "/heartbeat")
    with httpx.Client(timeout=3, trust_env=False) as client:
        for url in urls:
            client.get(url).raise_for_status()


def _show(record: dict) -> None:
    ws, biz = _endpoints(record)
    print(f"WS:  {ws}")
    print(f"Biz: {biz}")
    print(f"Auth: {record.get('auth_mode', 'none')}")
    if record.get("hymio_metrics_targets"):
        print(f"Hymio metrics target: {record['hymio_metrics_targets']}")
    for kind in _KINDS:
        for path in record["paths"][kind]:
            print(f"{kind}: {path}")


def _source_tree() -> None:
    if not (_COMPOSE.is_file() and (_ROOT / "Dockerfile").is_file()):
        raise ValueError("Oath source checkout is required to build the Docker image")


def main() -> None:
    parser = argparse.ArgumentParser(prog="oath")
    commands = parser.add_subparsers(dest="action", required=True)
    start = commands.add_parser("start", help="Build and start Oath in Docker")
    start.add_argument("--config", default=str(_DEFAULT_CONFIG), help="Path to oath.toml")
    start.add_argument(
        "--system-prompt", type=Path, help="Replace application instructions with a Markdown file"
    )
    start.add_argument(
        "--context-provider",
        default="",
        help="Authenticated application context provider URL for Worker",
    )
    start.add_argument(
        "--context-provider-token-file",
        type=Path,
        help="Bearer token file used only by Worker for the context provider",
    )
    start.add_argument("--name", default="oath", help="Local deployment name")
    start.add_argument(
        "--image-resolver",
        default="",
        help="Private image resolver URL; uses the context provider token file",
    )
    start.add_argument(
        "--hymio-metrics-target",
        type=Path,
        help="Persistent file_sd target JSON used by Prometheus for Hymio Backend",
    )
    auth_arguments(start)
    for kind in _KINDS:
        start.add_argument(f"--{kind}", action="append", default=[], metavar="DIR")
    for action, help_text in (
        ("restart", "Restart with the last configuration"),
        ("status", "Show services, endpoints and extension paths"),
        ("stop", "Stop services while retaining data"),
    ):
        command = commands.add_parser(action, help=help_text)
        command.add_argument("--name", default="oath", help="Local deployment name")
    args = parser.parse_args()
    try:
        if args.action == "start":
            _source_tree()
            _state_path(args.name)
            config = Path(args.config).expanduser().resolve()
            loaded = load_config(
                config,
                system_prompt_file=args.system_prompt.expanduser().resolve()
                if args.system_prompt
                else None,
            )
            paths = _paths(args)
            auth_file = (
                str(Path(args.auth_token_file).expanduser().resolve())
                if args.auth_token_file
                else ""
            )
            if bool(args.context_provider) != bool(args.context_provider_token_file):
                raise ValueError(
                    "--context-provider and --context-provider-token-file must be used together"
                )
            context_token = (
                args.context_provider_token_file.expanduser().resolve()
                if args.context_provider_token_file
                else None
            )
            if args.image_resolver and not context_token:
                raise ValueError("--image-resolver requires --context-provider-token-file")
            if context_token and not context_token.is_file():
                raise ValueError("Context provider token file does not exist")
            metrics_target = _metrics_target(args.hymio_metrics_target)
            environment = auth_environment(args.auth_mode, args.auth_url, auth_file)
            overlay_data = _overlay(paths, loaded.agent.system_prompt_file)
            for service in ("gateway", "biz"):
                target = overlay_data["services"].setdefault(
                    service, {"environment": {}, "volumes": []}
                )
                target["environment"].update(environment)
                if args.auth_mode == "remote":
                    target["extra_hosts"] = ["host.docker.internal:host-gateway"]
                if auth_file:
                    target["environment"]["OATH_AUTH_TOKEN_FILE"] = "/run/oath/auth-token"
                    target["volumes"].append(
                        {
                            "type": "bind",
                            "source": auth_file,
                            "target": "/run/oath/auth-token",
                            "read_only": True,
                        }
                    )
            if args.context_provider:
                worker = overlay_data["services"].setdefault(
                    "worker", {"environment": {}, "volumes": []}
                )
                worker["environment"].update(
                    {
                        "OATH_CONTEXT_PROVIDER_URL": args.context_provider,
                        "OATH_CONTEXT_PROVIDER_TOKEN_FILE": ("/run/oath/context-provider-token"),
                    }
                )
                if args.image_resolver:
                    worker["environment"]["OATH_IMAGE_RESOLVER_URL"] = args.image_resolver
                worker.setdefault("extra_hosts", ["host.docker.internal:host-gateway"])
                worker["volumes"].append(
                    {
                        "type": "bind",
                        "source": str(context_token),
                        "target": "/run/oath/context-provider-token",
                        "read_only": True,
                    }
                )
            overlay_text = yaml.safe_dump(overlay_data, sort_keys=False)
            digest = hashlib.sha256(overlay_text.encode()).hexdigest()[:12]
            overlay = _state_dir() / f"{args.name}.{digest}.compose.yaml"
            _write_private(overlay, overlay_text)
            record = {
                "version": 1,
                "name": args.name,
                "config": str(config),
                "compose": str(_COMPOSE),
                "overlay": str(overlay),
                "paths": paths,
                "auth_mode": args.auth_mode,
                "system_prompt_file": str(loaded.agent.system_prompt_file)
                if loaded.agent.system_prompt_file
                else None,
                "hymio_metrics_targets": metrics_target,
            }
            _run_docker("up", record)
            _healthy(record)
            _save(record)
            _show(record)
        elif args.action == "restart":
            record = _load(args.name)
            _source_tree()
            if record.get("system_prompt_file"):
                load_config(record["config"], system_prompt_file=Path(record["system_prompt_file"]))
            for kind, paths in record["paths"].items():
                for path in paths:
                    _validate_directory(path, kind)
            metrics_target = record.get("hymio_metrics_targets")
            if metrics_target and not Path(metrics_target).is_file():
                raise ValueError(f"Hymio metrics target file does not exist: {metrics_target}")
            _run_docker("up", record, recreate=True)
            _healthy(record)
            _show(record)
        elif args.action == "status":
            record = _load(args.name)
            _run_docker("status", record)
            _healthy(record)
            _show(record)
        else:
            record = _load(args.name)
            _run_docker("down", record)
            print("Oath stopped; volumes retained")
    except (ValueError, OSError, httpx.HTTPError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"oath: {error}\n")


if __name__ == "__main__":
    main()
