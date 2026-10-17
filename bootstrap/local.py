"""Local process supervision. Distributed deployments use their process manager."""

import argparse
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from bootstrap.common.config import load_config
from bootstrap.common.environment import load_environment

SERVICES = (
    "storage",
    "queue",
    "manager",
    "tools",
    "consumer",
    "worker",
    "gateway",
    "biz",
    "supervisor",
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="oath.toml")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    load_environment()
    config = load_config(args.config)
    root = config.data_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    pid_path = root / "launcher.pid"
    if args.stop:
        if not pid_path.exists():
            raise SystemExit("No local launcher is running")
        pid = int(pid_path.read_text())
        command = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True
        ).stdout
        if "-m bootstrap.local" not in command:
            raise SystemExit("Stale PID file; refusing to signal an unrelated process")
        os.kill(pid, signal.SIGTERM)
        return
    with (root / "launcher.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Local launcher already running") from None
        pid_path.write_text(str(os.getpid()))
        stopping = False

        def stop(*_):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        children = {}
        logs = {}
        retry_at = {}
        try:
            (root / "logs").mkdir(exist_ok=True)
            for name in SERVICES:
                logs[name] = (root / "logs" / f"{name}.log").open("ab", buffering=0)
            print(f"Oath services starting; logs: {root / 'logs'}", flush=True)
            while not stopping:
                for name in SERVICES:
                    child = children.get(name)
                    if child and child.poll() is None:
                        continue
                    if time.monotonic() < retry_at.get(name, 0):
                        continue
                    children[name] = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "bootstrap",
                            name,
                            "--config",
                            str(Path(args.config).resolve()),
                        ],
                        stdout=logs[name],
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    retry_at[name] = time.monotonic() + 3
                time.sleep(0.25)
        finally:
            for name in reversed(SERVICES):
                child = children.get(name)
                if child and child.poll() is None:
                    child.terminate()
            deadline = time.monotonic() + 15
            for child in children.values():
                try:
                    child.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            for log in logs.values():
                log.close()
            pid_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
