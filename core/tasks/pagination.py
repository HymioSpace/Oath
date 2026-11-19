"""Opaque pagination cursors bound to an authenticated account and query.

Cursors select a position, never confer authorization; every read remains owner-scoped.
"""

import base64
import hashlib
import json


def page(items, uid, query):
    limit = query.get("limit", 20)
    scope = {k: v for k, v in query.items() if k not in {"limit", "cursor"}}
    tag = hashlib.sha256(json.dumps([uid, scope], sort_keys=True).encode()).hexdigest()
    offset = 0
    if query.get("cursor"):
        try:
            token = json.loads(base64.urlsafe_b64decode(query["cursor"].encode()))
            if token["tag"] != tag or type(token["offset"]) is not int or token["offset"] < 0:
                raise ValueError("Cursor belongs to a different query")
            offset = token["offset"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("Invalid pagination cursor") from exc
    cursor = None
    if len(items) > offset + limit:
        cursor = base64.urlsafe_b64encode(
            json.dumps({"tag": tag, "offset": offset + limit}).encode()
        ).decode()
    return items[offset : offset + limit], cursor
