"""
fleet_agent/security.py
The agent listens on the operator's own machine, but "localhost only" is not a
boundary against a web page the operator happens to have open: any site can
make the browser call http://127.0.0.1:<port>, and DNS rebinding can even
change what a hostname resolves to. This agent can send flight commands, so it
checks three things on every request:

  1. Host header is exactly 127.0.0.1:<port> or localhost:<port>  (rebinding)
  2. Origin, when the browser sends one, is one of those same origins (CSRF)
  3. a per-launch random token, handed over once through the URL printed in the
     console and then kept in an HttpOnly SameSite=Strict cookie
"""
from __future__ import annotations

import hmac
import secrets

COOKIE = "fleet_agent_k"


class Guard:
    def __init__(self, port: int, token: str | None = None) -> None:
        self.port = port
        self.token = token or secrets.token_urlsafe(32)
        self._hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self._origins = {f"http://{h}" for h in self._hosts}

    def launch_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?k={self.token}"

    def token_ok(self, candidate: str | None) -> bool:
        return bool(candidate) and hmac.compare_digest(candidate, self.token)

    def check(self, *, host: str | None, origin: str | None, cookie_token: str | None,
              method: str = "GET", needs_token: bool = True) -> str | None:
        """Return None when the request is allowed, else a short reason."""
        if (host or "") not in self._hosts:
            return "bad host"
        if origin and origin not in self._origins:
            return "bad origin"
        if method.upper() not in ("GET", "HEAD", "OPTIONS") and not origin and not cookie_token:
            return "missing credentials"
        if needs_token and not self.token_ok(cookie_token):
            return "missing or wrong launch token"
        return None
