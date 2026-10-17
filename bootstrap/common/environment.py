"""Load local key=value settings without shell evaluation or credential logging."""

import os
from pathlib import Path


def load_environment(path=Path(".env")):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.isidentifier():
            raise ValueError("Invalid environment assignment")
        os.environ.setdefault(key, value)
