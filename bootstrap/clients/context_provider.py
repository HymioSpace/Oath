"""Bounded application context transport; runtime owns request freezing."""

import json
from pathlib import Path
from urllib.request import Request, urlopen


def provider(config):
    if not config.context_provider_url:
        return None

    def fetch(user, channel="unknown"):
        try:
            token = Path(config.context_provider_token_file).read_text().strip()
            request = Request(
                config.context_provider_url,
                data=json.dumps(
                    {
                        "user_id": user.user_id,
                        "agent_id": user.agent_id,
                        "channel": (
                            channel
                            if isinstance(channel, str) and 0 < len(channel) <= 32
                            else "unknown"
                        ),
                    }
                ).encode(),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            )
            with urlopen(request, timeout=8) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("Context response exceeded budget")
            value = json.loads(raw)
            text = value["content"]
            if not isinstance(text, str) or len(text) > 16000:
                raise ValueError("Invalid context")
            return text
        except Exception:
            return "Personal context is temporarily unavailable. Do not invent prior knowledge."

    return fetch
