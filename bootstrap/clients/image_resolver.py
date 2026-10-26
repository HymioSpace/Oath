"""Resolve owned opaque image references without persisting provider image payloads."""

import json
from pathlib import Path
from urllib.request import Request, urlopen

from core.protocol.error import LlmError


def resolver(config):
    if not config.image_resolver_url:
        return None

    def fetch(user_id, refs):
        if len(refs) > 12:
            raise LlmError("Too many native images; start with fewer attachments")
        try:
            token = Path(config.image_resolver_token_file).read_text().strip()
            request = Request(
                config.image_resolver_url,
                data=json.dumps({"user_id": user_id, "image_refs": refs}).encode(),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            )
            with urlopen(request, timeout=10) as response:
                raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ValueError("Image resolution exceeded budget")
            images = json.loads(raw)["images"]
            if (
                not isinstance(images, dict)
                or set(images) != set(refs)
                or any(
                    not isinstance(url, str)
                    or not url.startswith(
                        (
                            "data:image/jpeg;base64,",
                            "data:image/png;base64,",
                            "data:image/webp;base64,",
                        )
                    )
                    for url in images.values()
                )
            ):
                raise ValueError("Invalid image resolution response")
            return images
        except Exception as error:
            raise LlmError(
                "Cannot resolve owned images; no image was assumed understood"
            ) from error

    return fetch
