"""
fleet_agent/captain_link.py
Everything the agent says to the Capitan web server, over routes that already
exist plus the two small additions shipped in the captain patch:

    POST /auth/login, /auth/logout, GET /auth/me     login as the operator
    WS   /ws/agent                                   presence + joystick state
                                                     (turns agent-link-status and
                                                     joystick-status green)
    POST /command                                    commands, source:"agent"
    GET  /fleet/state                                drone ids for the target picker
    GET/POST /auth/users                             list / create accounts (admin UI)
    GET  /agent/settings, PUT /agent/settings/{s}    saved device settings   (new)
    GET  /agent/biometrics/me, PUT /agent/biometrics/{u}/{kind}             (new)

The session token is kept in memory only and sent as a Cookie header; the
browser showing the agent UI never holds it.
"""
from __future__ import annotations
import time
import asyncio
import json
import logging
import socket
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)


class CaptainError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


def normalize_url(url: str) -> str:
    u = urlparse((url or "").strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        raise CaptainError(0, "Captain address must look like http://192.168.1.20:8766")
    return f"{u.scheme}://{u.netloc}"


def _detail(resp: httpx.Response) -> str:
    try:
        d = resp.json().get("detail", "")
        return d if isinstance(d, str) else json.dumps(d)[:300]
    except Exception:
        return resp.text[:200] or f"HTTP {resp.status_code}"


def _ws_connect(url: str, headers: dict):
    """websockets ≥14 renamed the header argument; support both."""
    try:
        from websockets.asyncio.client import connect
        return connect(url, additional_headers=headers, open_timeout=6)
    except ImportError:
        import websockets
        return websockets.connect(url, extra_headers=headers, open_timeout=6)


def _handshake_status(exc: Exception) -> Optional[int]:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) or getattr(exc, "status_code", None)


class CaptainLink:
    def __init__(self, *, state_provider: Callable[[], dict], emit: Callable[[dict], None],
                state_interval_s: float = 5.0) -> None:
        self._state, self._emit, self._interval = state_provider, emit, state_interval_s
        self._client: Optional[httpx.AsyncClient] = None
        self._token: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._changed = asyncio.Event()
        self._nav_q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self.base_url: Optional[str] = None
        self.user: Optional[dict] = None
        self.presence = "off"

    # ── session ────────────────────────────────────────────────────────────

    @property
    def logged_in(self) -> bool:
        return self._token is not None and self.user is not None

    async def login(self, url: str, username: str, password: str) -> dict:
        await self.logout(quiet=True)
        base = normalize_url(url)
        client = httpx.AsyncClient(base_url=base, timeout=8.0)
        try:
            resp = await client.post("/auth/login", json={"username": username, "password": password})
        except httpx.HTTPError as exc:
            await client.aclose()
            raise CaptainError(0, f"Cannot reach Captain at {base}: {exc.__class__.__name__}") from None
        if resp.status_code != 200:
            await client.aclose()
            raise CaptainError(resp.status_code, "Invalid username or password"
                               if resp.status_code == 401 else _detail(resp))
        token = resp.cookies.get("session")
        if not token:
            await client.aclose()
            raise CaptainError(502, "Captain accepted the login but sent no session cookie")
        body = resp.json()
        self._client, self._token, self.base_url = client, token, base
        self.user = {"username": body.get("username", username), "role": body.get("role", "operator")}
        self._task = asyncio.create_task(self._presence_loop(), name="captain-presence")
        self._emit({"type": "session", "logged_in": True})
        return dict(self.user)

    async def logout(self, quiet: bool = False) -> None:
        task, self._task = self._task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._client and self._token:
            try:
                await self._client.post("/auth/logout", headers=self._cookie())
            except httpx.HTTPError:
                pass
        if self._client:
            await self._client.aclose()
        was = self.logged_in
        self._client = self._token = self.user = None
        self._set_presence("off")
        if was and not quiet:
            self._emit({"type": "session", "logged_in": False})

    async def verify_password(self, password: str) -> bool:
        """Re-check the operator's password without touching the main session:
        a throw-away login that is logged out again straight away."""
        if not (self._client and self.user):
            raise CaptainError(401, "Not logged in to Captain")
        async with httpx.AsyncClient(base_url=self.base_url, timeout=8.0) as c:
            try:
                r = await c.post("/auth/login", json={"username": self.user["username"], "password": password})
            except httpx.HTTPError as exc:
                raise CaptainError(0, f"Cannot reach Captain: {exc.__class__.__name__}") from None
            if r.status_code != 200:
                return False
            tok = r.cookies.get("session")
            if tok:
                try:
                    await c.post("/auth/logout", headers={"Cookie": f"session={tok}"})
                except httpx.HTTPError:
                    pass
            return True

    def _cookie(self) -> dict:
        return {"Cookie": f"session={self._token}"}

    async def _session_lost(self) -> None:
        if self._client is None:
            return
        log.warning("Captain session expired")
        self._emit({"type": "session.expired"})
        asyncio.create_task(self.logout(quiet=True))

    # ── requests ───────────────────────────────────────────────────────────

    async def request(self, method: str, path: str, **kw) -> Any:
        if not (self._client and self._token):
            raise CaptainError(401, "Not logged in to Captain")
        try:
            resp = await self._client.request(method, path, headers=self._cookie(), **kw)
        except httpx.HTTPError as exc:
            raise CaptainError(0, f"Captain unreachable: {exc.__class__.__name__}") from None
        if resp.status_code == 401:
            await self._session_lost()
            raise CaptainError(401, "Captain session expired — log in again")
        if resp.status_code >= 400:
            raise CaptainError(resp.status_code, _detail(resp))
        return resp.json() if resp.content else {}

    

    async def fleet_drones(self) -> list[dict]:
        data = await self.request("GET", "/fleet/state")
        return [{"drone_id": str(d.get("drone_id") or d.get("hardware_id")),
                 "hardware_id": d.get("hardware_id"), "roles": d.get("roles", [])}
                for d in data.get("drone_profiles", [])]
    
    async def send_command(self, cmd: dict) -> dict:                                                return await  self.request("POST", "/command", json=cmd)
    async def get_settings(self) -> dict:                                                           return (await self.request("GET", "/agent/settings")).get("settings", {})
    async def put_setting(self, scope: str, value: dict) -> dict:                                   return await  self.request("PUT", f"/agent/settings/{scope}", json={"value": value})
    async def my_biometrics(self) -> dict:                                                          return (await self.request("GET", "/agent/biometrics/me")).get("templates", {})
    async def put_biometric(self, username: str, kind: str, model: str, template: list) -> dict:    return await  self.request("PUT", f"/agent/biometrics/{username}/{kind}", json={"model": model, "template": template})
    async def delete_biometric(self, username: str, kind: str) -> dict:                             return await  self.request("DELETE", f"/agent/biometrics/{username}/{kind}")
    async def list_users(self) -> list[dict]:                                                       return await  self.request("GET", "/auth/users")
    async def create_user(self, username: str, password: str, role: str) -> dict:                   return await  self.request("POST", "/auth/users", json={"username": username, "password": password, "role": role})
 
    async def update_user(self, user_id: int, *, password: Optional[str] = None, role: Optional[str] = None, active: Optional[bool] = None) -> dict:
        body = {}
        if password is not None: body["password"] = password
        if role is not None:     body["role"]     = role
        if active is not None:   body["active"]   = active
        if not body:
            raise CaptainError(422, "no fields to update")
        return await self.request("PATCH", f"/auth/users/{user_id}", json=body)

    async def delete_user(self, user_id: int) -> dict:
        return await self.request("DELETE", f"/auth/users/{user_id}")

    async def list_users_with_biometrics(self) -> list[dict]:
        """Users + enrolled biometric status, for the agent Register tab."""
        return await self.request("GET", "/agent/biometrics/users")
   
    def notify_state_changed(self) -> None:
        self._changed.set()

    def _set_presence(self, value: str) -> None:
        if value != self.presence:
            self.presence = value
            self._emit({"type": "presence", "state": value})

    async def _send_state(self, ws) -> None:
        js = self._state()
        payload: dict = {
            "type": "state",
            "device": socket.gethostname()[:64],
            "joystick": {
                "present": bool(js.get("present")),
                "name":    js.get("name", ""),
                "count":   int(js.get("count", 0)),
            },
        }
        nav = js.get("nav_mode")
        if isinstance(nav, bool):
            payload["nav_mode"] = nav
        await ws.send(json.dumps(payload))

    async def _presence_loop(self) -> None:
        backoff = 1.0
        while self._token:
            url = ("wss" if self.base_url.startswith("https") else "ws") + \
                self.base_url[self.base_url.index("://"):] + "/ws/agent"
            try:
                self._set_presence("connecting")
                async with _ws_connect(url, self._cookie()) as ws:
                    self._set_presence("up")
                    backoff = 1.0
                    self._changed.clear()
                    await self._send_state(ws)
                    last_state = time.monotonic()
                    while True:
                        # Drain queued nav actions first (ordered, sub-100 ms latency).
                        while True:
                            try:
                                item = self._nav_q.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            await ws.send(json.dumps(item))
                        # Periodic or on-change state frame.
                        now = time.monotonic()
                        if self._changed.is_set() or (now - last_state) >= self._interval:
                            self._changed.clear()
                            await self._send_state(ws)
                            last_state = now
                        await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info("presence link down: %s", exc)
                self._set_presence("retrying")
                if _handshake_status(exc) in (401, 403):
                    try:
                        await self.request("GET", "/auth/me")
                    except CaptainError:
                        pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)


    def send_nav(self, action: str, drone_id: Optional[str] = None, **extra) -> None:
        """Enqueue a discrete UI nav action (or an axis snapshot) for /ws/agent.

        `extra` is merged into the frame verbatim — used for `x/y/rx/ry` on the
        `axes` action. Silent when presence is down — a hat nudge is not worth
        queueing across a reconnect.
        """
        if self.presence != "up":
            return
        msg = {"type": "nav", "action": str(action)[:32]}
        if drone_id is not None:
            msg["drone_id"] = str(drone_id)[:64]
        for k, v in extra.items():
            if k in ("x", "y", "rx", "ry") and isinstance(v, (int, float)):
                msg[k] = round(float(v), 3)
        try:
            self._nav_q.put_nowait(msg)
        except asyncio.QueueFull:
            log.debug("nav queue full — dropping %s", action)
