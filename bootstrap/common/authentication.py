"""用户凭据与短期 CLI capability；内部服务凭据不进入 Bash 环境。"""

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import httpx

from core.protocol.datatype.user import User
from core.protocol.error import PermissionDeniedError, UserError
from core.protocol.interface.identity import UserAuthenticator as Authenticator
from core.protocol.interface.identity import UserIdentity


def service_token(config):
    token = config.services.internal_token
    if len(token) < 32:
        raise ValueError("Set services.internal_token to at least 32 characters")
    return token


def auth_arguments(parser, *, default="none"):
    parser.add_argument("--auth-mode", choices=("none", "static", "remote"), default=default)
    parser.add_argument("--auth-url", default="")
    parser.add_argument("--auth-token-file", default="")


def auth_environment(mode, url, token_file):
    if mode == "remote":
        if not url.startswith(("http://", "https://")) or not token_file:
            raise ValueError("remote auth requires --auth-url and --auth-token-file")
        if len(Path(token_file).read_text().strip()) < 32:
            raise ValueError("Auth service credential must contain at least 32 characters")
    elif url or token_file:
        raise ValueError("Auth URL and credential file require --auth-mode remote")
    return {"OATH_AUTH_MODE": mode, "OATH_AUTH_URL": url, "OATH_AUTH_TOKEN_FILE": token_file}


class UserAuthenticator(Authenticator):
    def __init__(self, config, *, cli=False):
        self.users = config.services.users
        self.secret = service_token(config)
        self.cli = cli
        self.mode = os.environ.get("OATH_AUTH_MODE", "none")
        if self.mode not in {"none", "static", "remote"}:
            raise ValueError("Invalid OATH_AUTH_MODE")
        self.url = os.environ.get("OATH_AUTH_URL", "")
        self.token_file = os.environ.get("OATH_AUTH_TOKEN_FILE", "")
        if self.mode == "remote":
            auth_environment(self.mode, self.url, self.token_file)
        if not isinstance(self.users, dict):
            raise ValueError("services.users must be a token-to-user_id table")
        for uid in self.users.values():
            User(uid)

    def authenticate(self, credential, user_id=None):
        if self.cli:
            return UserIdentity(self._credential(credential).user_id)
        if self.mode == "none":
            try:
                return UserIdentity(User(user_id).user_id)
            except (ValueError, TypeError, UserError) as exc:
                raise PermissionDeniedError("A valid user_id is required") from exc
        if self.mode == "static":
            return UserIdentity(self._credential(credential).user_id)
        if not isinstance(credential, str) or not credential or len(credential) > 8192:
            raise PermissionDeniedError("Invalid credential")
        try:
            token = Path(self.token_file).read_text().strip()
            with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
                response = client.post(
                    self.url,
                    headers={"Authorization": f"Bearer {token}"},
                    json={"credential": credential},
                )
                response.raise_for_status()
                data = response.json()
            expiry = float(data["expires_at"])
            if not time.time() < expiry < float("inf"):
                raise ValueError("Expired")
            return UserIdentity(User(data["user_id"]).user_id, expiry)
        except Exception as exc:
            raise PermissionDeniedError(
                "Credential rejected or authentication unavailable"
            ) from exc

    def _credential(self, credential):
        if not isinstance(credential, str) or not credential:
            raise PermissionDeniedError("Missing credential")
        if not self.cli or self.mode == "static":
            for token, uid in self.users.items():
                if hmac.compare_digest(token.encode(), credential.encode()):
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
