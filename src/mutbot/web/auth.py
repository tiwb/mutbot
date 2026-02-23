"""Token-based authentication manager."""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import Any

from mutbot import storage

logger = logging.getLogger(__name__)

# Token TTL: 7 days
TOKEN_TTL = 7 * 24 * 3600


class AuthManager:
    """Simple session-token authentication.

    Credentials are loaded from ``.mutbot/config.json`` under ``auth.users``::

        {
          "auth": {
            "users": {
              "admin": "password123"
            }
          }
        }

    When no ``auth.users`` section exists (or is empty), authentication is
    disabled entirely.  Connections from localhost are also allowed to bypass
    auth when ``auth.skip_localhost`` is true (default).
    """

    def __init__(self) -> None:
        self._users: dict[str, str] = {}  # username → password
        self._tokens: dict[str, tuple[str, float]] = {}  # token → (username, expires_at)
        self.skip_localhost: bool = True
        self.enabled: bool = False

    def load_config(self) -> None:
        """Load auth config from .mutbot/config.json."""
        data = storage.load_json(storage._mutbot_path("config.json"))
        if not data:
            self.enabled = False
            return
        auth_section = data.get("auth", {})
        self._users = auth_section.get("users", {})
        self.skip_localhost = auth_section.get("skip_localhost", True)
        self.enabled = bool(self._users)
        if self.enabled:
            logger.info("Auth enabled with %d user(s)", len(self._users))
        else:
            logger.info("Auth disabled (no users configured)")

    def verify_credentials(self, username: str, password: str) -> str | None:
        """Verify username/password, return session token or None."""
        expected = self._users.get(username)
        if expected is None or expected != password:
            return None
        token = secrets.token_urlsafe(32)
        self._tokens[token] = (username, time.time() + TOKEN_TTL)
        logger.info("Auth: user '%s' logged in", username)
        return token

    def verify_token(self, token: str) -> str | None:
        """Verify a session token, return username or None."""
        entry = self._tokens.get(token)
        if entry is None:
            return None
        username, expires_at = entry
        if time.time() > expires_at:
            del self._tokens[token]
            return None
        return username

    def should_skip_auth(self, host: str) -> bool:
        """Return True if auth should be skipped for this host."""
        if not self.enabled:
            return True
        if not self.skip_localhost:
            return False
        # Check common localhost representations
        if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return True
        if host.startswith("127."):
            return True
        return False
