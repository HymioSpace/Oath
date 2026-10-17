"""用户凭据与短期 CLI capability；内部服务凭据不进入 Bash 环境。"""

import base64
import hashlib
import hmac
import json
import time

from core.protocol.datatype.user import User
from core.protocol.error import PermissionDeniedError


def service_token(config):
    token = config.services.internal_token
    if len(token) < 32:
        raise ValueError("Set services.internal_token to at least 32 characters")
    return token


class UserAuthenticator:
    def __init__(self, config, *, cli=False):
        self.users = config.services.users
        self.secret = service_token(config)
        self.cli = cli
        if not isinstance(self.users, dict):
            raise ValueError("services.users must be a token-to-user_id table")
        for uid in self.users.values():
            User(uid)

    def authenticate(self, credential):
        if not credential:
            raise PermissionDeniedError("Missing credential")
        for token, uid in self.users.items():
            if hmac.compare_digest(token, credential):
                return User(uid)
        if self.cli:
            try:
                body, signature = credential.split(".")
                expected = hmac.new(self.secret.encode(), body.encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected, signature):
                    raise ValueError()
                data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
                if data["aud"] != "cli" or data["exp"] <= time.time():
                    raise ValueError()
                return User(data["user_id"])
            except (ValueError, KeyError, TypeError):
                pass
        raise PermissionDeniedError("Unknown credential")


def cli_token(config, user):
    body = (
        base64.urlsafe_b64encode(
            json.dumps({"user_id": user.user_id, "aud": "cli", "exp": time.time() + 3600}).encode()
        )
        .decode()
        .rstrip("=")
    )
    signature = hmac.new(service_token(config).encode(), body.encode(), hashlib.sha256).hexdigest()
    return body + "." + signature
