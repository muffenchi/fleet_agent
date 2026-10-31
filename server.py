"""
fleet_agent/server.py
The agent's own web server (127.0.0.1 only) — serves the agent WebUI from
www/ and exposes /api/* and /ws/ui to it. All Captain traffic goes through
CaptainLink; the browser never talks to Captain from this page and never
sees the Captain session token.
"""
from __future__ import annotations
import logging
import asyncio
import base64
import binascii
import os
import sys
import time
from pathlib import Path
from logging.handlers import RotatingFileHandler

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "fleet_agent"

# ─────────────────────────────────────────────────────────────────────────────
# 1. imports — next to the other `from .x import y` lines at the top of server.py
# ─────────────────────────────────────────────────────────────────────────────
from .voice_trace import TRACE, TraceLog, new_trace

from typing import Callable, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .              import settings_schema as schema
from .              import voice_commands
from .biometrics    import BiometricError, EnrollmentStore, verify
from .captain_link  import CaptainError, CaptainLink
from .commands      import ACTIONS, PSEUDO_ACTIONS, build_action   # add PSEUDO_ACTIONS
from .config        import Config
from .joystick      import JoystickMapper, JoystickMonitor, PygameBackend
from .security      import COOKIE, Guard
from .voice         import VoiceController, VoiceGate, _VERBS

WWW = Path(__file__).resolve().parent / "www"
MAX_B64 = 6_000_000      # ~4.5 MB decoded; a 3 s WAV is ~100 KB, a JPEG frame ~150 KB

LOG_FILE = Path(os.environ.get(
    "FLEET_AGENT_LOG_FILE", str(Path.home() / ".fleet_agent" / "fleet_agent.log")
)).expanduser()


def _configure_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(handler, RotatingFileHandler)
               and Path(handler.baseFilename) == LOG_FILE.resolve()
               for handler in root.handlers):
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=5_000_000, backupCount=4, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        ))
        root.addHandler(file_handler)
    if not any(isinstance(handler, logging.StreamHandler)
               and not isinstance(handler, logging.FileHandler)
               for handler in root.handlers):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        ))
        root.addHandler(console_handler)


_configure_logging()
log = logging.getLogger(__name__)
log.info("file logging enabled path=%s", LOG_FILE.resolve())


class DispatchError(Exception):
    pass


class Hub:
    """Fan-out of agent events to every open UI socket."""
    def __init__(self) -> None:
        self._qs: set[asyncio.Queue] = set()
        self.rev = 0          # stamped on every event and status snapshot so the UI can drop stale ones

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._qs.add(q)
        return q

    def unsubscribe(self, q) -> None:
        self._qs.discard(q)

    def emit(self, ev: dict) -> None:
        self.rev += 1
        ev["rev"] = self.rev
        for q in list(self._qs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass


class AttemptLimiter:
    """Agent-side brake on unlock guessing: `limit` failures inside `window_s`
    lock unlock attempts for `cooldown_s`. Captain's own /auth/login is the real
    password check; this just stops the voice-unlock screen being a free oracle."""
    def __init__(self, limit=5, window_s=300, cooldown_s=60, clock=time.monotonic) -> None:
        self.limit, self.window_s, self.cooldown_s, self._clock = limit, window_s, cooldown_s, clock
        self._fails: list[float] = []
        self._blocked_until = 0.0

    def blocked_for(self) -> int:
        return max(0, int(self._blocked_until - self._clock() + 0.999))

    def fail(self) -> None:
        now = self._clock()
        self._fails = [t for t in self._fails if now - t < self.window_s] + [now]
        if len(self._fails) >= self.limit:
            self._blocked_until, self._fails = now + self.cooldown_s, []

    def reset(self) -> None:
        self._fails, self._blocked_until = [], 0.0



def _b64(data: Optional[str]) -> bytes:
    if not data or len(data) > MAX_B64:
        raise HTTPException(422, "sample missing or too large")
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "sample is not valid base64") from None



class Agent:
    def __init__(self, cfg: Config, guard: Guard, engines: dict, backend_factory: Callable) -> None:
        self.cfg, self.guard, self.engines, self._backend_factory = cfg, guard, engines, backend_factory
        self.hub = Hub()
        self.settings = {s: schema.merged(s, None) for s in schema.SCOPES}
        self.saved_ts: dict[str, Optional[int]] = {s: None for s in schema.SCOPES}
        self.settings_storage = True              # False when Captain has no /agent/settings (patch missing)
        self.drones: list[dict] = []
        self.target: Optional[str] = None
        self.gate = VoiceGate(self.settings["voice"]["unlock_ttl_s"])
        self.limiter = AttemptLimiter()
        self.enrollments = EnrollmentStore()
        self.traces = TraceLog()
        self.pending_trace: Optional[str] = None   # trace id of the sentence awaiting "confirm"
        self.monitor: Optional[JoystickMonitor] = None
        self.pad_watchers = 0
        self.link = CaptainLink(
                                state_provider=lambda: ({
                                    **(self.monitor.info if self.monitor else {}),
                                    "nav_mode": self.mapper.nav_mode,          # ← add this
                                }),
                                emit=self._on_link_event,
                                state_interval_s=cfg.state_interval_s,
                            )
        self.mapper = JoystickMapper(
                                        settings=lambda: self.settings["joystick"],
                                        present=lambda: bool(self.monitor and self.monitor.info["present"]),
                                        dispatch=lambda action: self.execute(action, None, "joystick"),
                                        emit=self.hub.emit,
                                        link=self.link,
                                        on_voice_toggle=self._toggle_voice_gate,
                                        )
        self.voice = VoiceController(
                                        self.gate,
                                        settings=lambda: self.settings["voice"],
                                        known_drones=lambda: [d["drone_id"] for d in self.drones],
                                        default_drone=lambda: self.target,
                                        execute=self.execute,
                                        local_matcher=self._match_local_voice,
                                    )
        self._tasks: list[asyncio.Task] = []

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.monitor = JoystickMonitor(
            self._backend_factory, loop,
            on_presence=self._on_pads,
            on_edge=self.mapper.on_edge,
            on_hat=self.mapper.on_hat,
            on_axes=self.mapper.on_axes,
        )
        self.monitor.start()
        self._tasks = [asyncio.create_task(self._gate_watch()), asyncio.create_task(self._pad_stream())]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.monitor:
            await run_in_threadpool(self.monitor.stop)
        await self.link.logout(quiet=True)

    async def _gate_watch(self) -> None:
        last = self.gate.unlocked
        while True:
            await asyncio.sleep(1)
            if self.gate.unlocked != last:
                last = self.gate.unlocked
                self.hub.emit({"type": "voice.gate", **self.gate.snapshot()})

    async def _toggle_voice_gate(self) -> dict:
        """Physical button (default: 10) toggles the voice gate. Called from
        JoystickMapper — the return value is emitted as `voice.gate.toggle` so
        the UI can show the reason. The gate's own `_gate_watch` loop will
        also broadcast `voice.gate` on state change."""
        result = self.gate.toggle(grace_s=90.0)
        self.hub.emit({"type": "voice.gate", **self.gate.snapshot()})
        return result


    async def _pad_stream(self) -> None:
        last = None
        while True:
            await asyncio.sleep(0.1)
            if self.pad_watchers <= 0 or not self.monitor:
                continue
            pads = [{"id": p.id, "name": p.name, "axes": p.axes, "buttons": p.buttons, "hats": p.hats}
                    for p in self.monitor.pads]
            if pads != last:
                last = pads
                self.hub.emit({"type": "joystick.state", "pads": pads})

    # ── events ─────────────────────────────────────────────────────────────

    def _on_pads(self, info: dict) -> None:
        self.link.notify_state_changed()
        self.hub.emit({"type": "joystick.presence", **info})

    def _on_link_event(self, ev: dict) -> None:
        self.hub.emit(ev)
        if ev.get("type") == "session.expired" or (ev.get("type") == "session" and not ev.get("logged_in")):
            self._reset_session_state()

    def _reset_session_state(self) -> None:
        self.gate.lock()
        self.settings = {s: schema.merged(s, None) for s in schema.SCOPES}
        self.saved_ts = {s: None for s in schema.SCOPES}
        self.drones, self.target = [], None
        self.enrollments = EnrollmentStore()
        self.hub.emit({"type": "voice.gate", **self.gate.snapshot()})

    # ── status ─────────────────────────────────────────────────────────────

    def status(self) -> dict:
        info = self.monitor.info if self.monitor else {"present": False, "name": "", "count": 0}
        return {
            "rev": self.hub.rev,
            "captain": {"url": self.link.base_url or self.cfg.captain_url,
                        "logged_in": self.link.logged_in,
                        "user": self.link.user,
                        "presence": self.link.presence},
            "joystick": {**info, "nav_mode": self.mapper.nav_mode,
                        "error": self.monitor.backend_error if self.monitor else None},
            "voice":    {"gate": self.gate.snapshot(), "pending": self.voice.pending},
            "engines":  {k: {"available": v["engine"] is not None, "reason": v["reason"],
                            "needed_samples": getattr(v["engine"], "needed_samples", None)}
                        for k, v in self.engines.items()},
            "drones": self.drones, "target": self.target,
            "settings": self.settings, "saved_ts": self.saved_ts,
            "settings_storage": self.settings_storage,
            "actions": {
                n: {"name": a.name, "label": a.label, "command": a.command,
                    "params": a.params, "confirm": a.confirm,
                    "voice_phrases": list(_VERBS.get(n, (n,)))}
                for n, a in ACTIONS.items()
            },
            "pseudo_actions": {
                "nav_toggle":      {"label": "Toggle joystick navigation"},
                "select_drone":    {"label": "Select focused drone (open panel)"},
                "deselect_drone":  {"label": "Clear drone selection"},
                "focus_next":      {"label": "Focus next drone"},
                "focus_prev":      {"label": "Focus previous drone"},
                "context_menu":    {"label": "Open right-click menu"},
                "zoom_in":         {"label": "Map zoom in"},
                "zoom_out":        {"label": "Map zoom out"},
            },
        }
    # )── Captain-backed operations ──────────────────────────────────────────

    def _sync_engine_thresholds(self) -> None:
        v = self.settings.get("verification", {})
        face_eng = self.engines["face"]["engine"]
        voice_eng = self.engines["voice"]["engine"]
        if voice_eng is not None:
            if "voice_threshold" in v:  voice_eng.threshold = v["voice_threshold"]
            if "min_voice_audio_s" in v:
                setattr(voice_eng, "min_audio_s", v["min_voice_audio_s"])
        if face_eng is not None and "face_threshold" in v:
            face_eng.threshold = v["face_threshold"]


    def _match_local_voice(self, text: str) -> Optional[dict]:
        if not self.link.user:
            return None
        try:
            return voice_commands.match(self.link.user["username"], text)
        except Exception:
            return None

    async def login(self, url: str, username: str, password: str) -> None:
        await self.link.login(url, username, password)
        await self.restore_settings()
        try:
            await self.refresh_drones()
        except CaptainError:
            pass

    async def restore_settings(self) -> None:
        try:
            stored = await self.link.get_settings()
            self.settings_storage = True
        except CaptainError as exc:
            if exc.status == 404:
                self.settings_storage = False
            self.hub.emit({"type": "warning", "detail": f"Could not restore saved settings: {exc.detail}"})
            stored = {}
        for scope in schema.SCOPES:
            entry = stored.get(scope) or {}
            self.settings[scope] = schema.merged(scope, entry.get("value"))
            self.saved_ts[scope] = entry.get("updated_ts_us")
        self.gate.ttl_s = self.settings["voice"]["unlock_ttl_s"]
        self.hub.emit({"type": "settings", "settings": self.settings, "saved_ts": self.saved_ts})
        self._sync_engine_thresholds()

    async def refresh_drones(self) -> list[dict]:
        self.drones = await self.link.fleet_drones()
        if self.target and self.target not in [d["drone_id"] for d in self.drones]:
            self.target = None
        self.hub.emit({"type": "drones", "drones": self.drones, "target": self.target})
        return self.drones

    async def save_setting(self, scope: str, value: dict, password: Optional[str] = None) -> dict:
        cleaned = schema.clean(scope, value)
        new = {**self.settings[scope], **cleaned}
        if scope == "voice" and "unlock_factors" in cleaned:
            await self._check_factor_change(set(self.settings["voice"]["unlock_factors"]), set(cleaned["unlock_factors"]), password)
        res = await self.link.put_setting(scope, new)         # Captain first; apply only if it stuck
        self.settings[scope], self.saved_ts[scope] = new, res.get("updated_ts_us")
        if scope == "voice":
            self.gate.ttl_s = new["unlock_ttl_s"]
        self.hub.emit({"type": "settings", "settings": self.settings, "saved_ts": self.saved_ts})
        if scope == "verification":
            self._sync_engine_thresholds()
        if scope == "voice":
            logged_settings = {key: val for key, val in new.items() if key != "microphone_id"}
            log.info("voice settings saved settings=%s", logged_settings)
        else:
            log.info("settings saved scope=%s keys=%s", scope, sorted(cleaned))
        return new

    async def _check_factor_change(self, old: set, new: set, password: Optional[str]) -> None:
        """Removing an unlock factor weakens the gate: allowed only while it is unlocked, or
        with the Captain password. Adding one must be satisfiable, or the operator would lock
        themselves out of voice control with no way back."""
        if not old <= new and not self.gate.unlocked:
            wait = self.limiter.blocked_for()
            if wait:
                raise HTTPException(429, f"Too many failed attempts — try again in {wait} s")
            if not (password and await self.link.verify_password(password)):
                if password:
                    self.limiter.fail()
                raise HTTPException(403, "Unlock voice control, or enter your Captain password, before removing an unlock factor")
        added = new - old
        if added - {"password"}:
            stored = await self.link.my_biometrics()
            for factor in added:
                kind = {"voiceprint": "voice", "face": "face"}.get(factor)
                if kind is None:
                    continue
                if self.engines[kind]["engine"] is None:
                    raise schema.SettingsError(f"{factor} is not available on this laptop: {self.engines[kind]['reason']}")
                if kind not in stored:
                    raise schema.SettingsError(f"no {kind} template is enrolled for {self.link.user['username']} — "
                                               "ask a Captain admin to enrol you first")

# ── tracing ────────────────────────────────────────────────────────────

    def _trace_begin(self, tid: str, text: str, engine: str, count: bool = True) -> None:
        ev = self.traces.begin(tid, text, engine, count=count)
        self.hub.emit({"type": "voice.trace", "trace": tid, "stage": "heard", **ev})

    def _trace(self, tid: str, stage: str, **fields) -> None:
        ev = self.traces.stage(tid, stage, **fields)
        self.hub.emit({"type": "voice.trace", "trace": tid, "stage": stage, **ev})

    async def execute(self, action: str, drone_id: Optional[str], source: str) -> dict:
        tid = TRACE.get() or new_trace("js" if source == "joystick" else "ag")
        if not self.link.logged_in:
            refusal = "Not logged in to Captain"
        elif source == "voice" and not self.gate.unlocked:
            refusal = "Voice control is locked"
        elif source == "joystick" and not (self.monitor and self.monitor.info["present"]):
            refusal = "No joystick detected"
        else:
            refusal = None
        if refusal:
            self._trace(tid, "send", ok=False, action=action, source=source, detail=refusal)
            raise DispatchError(refusal)

        # NOTE: no `drone is None` refusal — Captain decides which drone to target.
        drone = drone_id or self.target or ""
        cmd = build_action(action, drone, source_hint=source)
        cmd["trace_id"] = tid
        self._trace(tid, "send", ok=True, action=action, drone=drone or "(auto)", source=source)
        log.info("sending command to Captain via POST /command: %s source=%s trace=%s drone=%r",
                cmd["label"], source, tid, drone or "(auto)")
        t0 = time.monotonic()
        try:
            res = await self.link.send_command(cmd)
        except CaptainError as exc:
            ms = int((time.monotonic() - t0) * 1000)
            # Friendly translation for the two common Captain-side refusals.
            friendly = exc.detail
            if "missing droneId" in (exc.detail or ""):
                friendly = "No drone selected on Captain — click a drone on the map, then repeat the command"
            self._trace(tid, "captain", ok=False, http=exc.status, ms=ms, detail=friendly)
            self.hub.emit({"type": "command.result", "ok": False, "label": cmd["label"],
                        "source": source, "detail": friendly, "trace": tid})
            raise DispatchError(friendly) from None
        ms = int((time.monotonic() - t0) * 1000)
        echoed = res.get("trace") if isinstance(res, dict) else None
        self._trace(tid, "captain", ok=True, http=200, ms=ms, linked=(echoed == tid), echoed=echoed)
        detail = f"{cmd['label']} accepted by Captain"
        log.info("Captain accepted command via POST /command: %s source=%s trace=%s (%d ms)",
                cmd["label"], source, tid, ms)
        self.hub.emit({"type": "command.result", "ok": True, "label": cmd["label"],
                    "source": source, "detail": detail, "trace": tid, "ms": ms})
        return {"detail": detail, "result": res.get("result"), "trace": tid}


# ─────────────────────────────────────────────────────────────────────────────
# 2. request bodies — REPLACE the existing TranscriptBody, ADD SttEventBody
# ─────────────────────────────────────────────────────────────────────────────

class SttEventBody(BaseModel):
    kind: str                             # "started" | "stopped" | "error"
    detail: str = ""
    engine: str = ""

# ── request bodies ───────────────────────────────────────────────────────────

class LoginBody(BaseModel):    
    url: str; 
    username: str; 
    password: str
class TargetBody(BaseModel):   
    drone_id: Optional[str] = None
class NavModeBody(BaseModel):
    on: bool
class SettingBody(BaseModel):
    value: dict
    password: Optional[str] = None
class TranscriptBody(BaseModel):
    text: str
    final: bool = True
    engine: Optional[str] = None          # which browser engine produced it (for the trace)
class UnlockBody(BaseModel):
    password: Optional[str] = None
    voice_wav_b64: Optional[str] = None
    face_jpeg_b64: Optional[str] = None
class NewUserBody(BaseModel):  
    username: str; 
    password: str; 
    role: str = "operator"
class UpdateUserBody(BaseModel):
    password: Optional[str] = None
    role:     Optional[str] = None
class EnrolBody(BaseModel):    
    username: str; 
    kind: str
class SampleBody(BaseModel):   
    session_id: str; 
    data_b64: str
class SessionBody(BaseModel):  
    session_id: str
class CommandBody(BaseModel):
    action: str
    drone_id: Optional[str] = None
    takeoff_alt_m: Optional[float] = None
    custom_command: Optional[int] = None
    params: Optional[dict] = None
    label: Optional[str] = None
class VoiceCommandBody(BaseModel):
    phrase: str
    action: str
    voice_wav_b64: Optional[str] = None
class PreviewBody(BaseModel):
    data_b64: str
class ConfirmBody(BaseModel):
    ok: bool
    then_action: Optional[str] = None
class VoiceAssignBody(BaseModel):
    action:    str
    phrase:    str
    data_b64:  str
    face_jpeg_b64: Optional[str] = None
    dry_run:       bool = False
class VoicePrecheckBody(BaseModel):
    data_b64:      str
    face_jpeg_b64: Optional[str] = None

def create_app(cfg: Config, *, engines: Optional[dict] = None, backend_factory: Callable = PygameBackend, guard: Optional[Guard] = None):
    if engines is None:
        from .engines import load_engines
        engines = load_engines()
    guard = guard or Guard(cfg.port)
    agent = Agent(cfg, guard, engines, backend_factory)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        agent.start()
        yield
        await agent.stop()

    app = FastAPI(title="Fleet Agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard_mw(request: Request, call_next):
        started = time.monotonic()
        host, origin = request.headers.get("host"), request.headers.get("origin")

        if request.url.path == "/" and "k" in request.query_params:     # the one-time entry point
            why = guard.check(host=host, origin=origin, cookie_token=request.query_params["k"],
                              method=request.method)
            if why:
                resp = PlainTextResponse(f"Forbidden: {why}", 403)
            else:
                resp = RedirectResponse("/", 303)
                resp.set_cookie(COOKIE, guard.token, httponly=True, samesite="strict")
        else:
            why = guard.check(host=host, origin=origin, cookie_token=request.cookies.get(COOKIE),
                              method=request.method)
            if why:
                resp = PlainTextResponse(
                    f"Forbidden: {why}. Open the link printed in the agent's console.", 403)
            else:
                resp = await call_next(request)

        elapsed_ms = (time.monotonic() - started) * 1000
        if resp.status_code >= 500:
            log.warning("%s %s -> %d (%.1f ms)", request.method, request.url.path, resp.status_code, elapsed_ms)
        else:
            log.info("%s %s -> %d (%.1f ms)", request.method, request.url.path, resp.status_code, elapsed_ms)
        return resp
  
    def captain_errors(fn):
        """CaptainError / DispatchError / SettingsError / BiometricError → HTTP errors."""
        import functools

        @functools.wraps(fn)
        async def wrapper(*a, **kw):
            try:
                return await fn(*a, **kw)
            except CaptainError as exc:
                raise HTTPException(exc.status if exc.status in (401, 403, 404, 409, 422) else 502, exc.detail)
            except DispatchError as exc:
                raise HTTPException(409, str(exc))
            except schema.SettingsError as exc:
                raise HTTPException(422, str(exc))
            except BiometricError as exc:
                raise HTTPException(422, str(exc))
        return wrapper

    def need_login():
        if not agent.link.logged_in:
            raise HTTPException(401, "Log in to Captain first")

    def need_admin():
        need_login()
        if agent.link.user["role"] != "admin":
            raise HTTPException(403, "Only a Captain admin can register users")

    # ── pages ──────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (WWW / "index.html").read_text(encoding="utf-8")

    app.mount("/static", StaticFiles(directory=WWW), name="www")

    # ── session ────────────────────────────────────────────────────────────

    @app.get("/api/status")
    async def status():
        return agent.status()

    @app.post("/api/login")
    @captain_errors
    async def login(body: LoginBody):
        await agent.login(body.url, body.username, body.password)
        return agent.status()

    @app.post("/api/logout")
    @captain_errors
    async def logout():
        await agent.link.logout()
        return agent.status()

    @app.get("/api/drones")
    @captain_errors
    async def drones():
        need_login()
        await agent.refresh_drones()
        return {"drones": agent.drones, "target": agent.target}

    @app.post("/api/target")
    async def target(body: TargetBody):
        need_login()
        ids = [d["drone_id"] for d in agent.drones]
        if body.drone_id and ids and body.drone_id not in ids:
            raise HTTPException(422, f"unknown drone {body.drone_id!r}")
        agent.target = body.drone_id or None
        agent.hub.emit({"type": "drones", "drones": agent.drones, "target": agent.target})
        return {"target": agent.target}

    @app.get("/api/commands")
    @captain_errors
    async def get_commands():
        need_login()
        js_bindings = agent.settings.get("joystick", {}).get("bindings", {})
        action_to_buttons: dict[str, list[str]] = {}
        for btn, act in js_bindings.items():
            action_to_buttons.setdefault(act, []).append(f"Button {btn}")

        wake_word = agent.settings.get("voice", {}).get("wake_word", "captain")
        req_wake = agent.settings.get("voice", {}).get("require_wake_word", True)

        rows = []
        for name, act in ACTIONS.items():
            phrases = list(_VERBS.get(name, (name,)))
            if req_wake and wake_word:
                voice_triggers = [f"{wake_word} {p}" for p in phrases]
            else:
                voice_triggers = list(phrases)

            buttons = action_to_buttons.get(name, [])

            rows.append({
                "name": name,
                "label": act.label,
                "command": act.command,
                "params": act.params,
                "confirm": act.confirm,
                "joystick_buttons": buttons,
                "voice_triggers": voice_triggers,
                "takeoff_alt_m": agent.settings.get("joystick", {}).get("takeoff_alt_m", 25) if name == "takeoff" else None,
            })

        return {
            "commands": rows,
            "target": agent.target,
            "drones": agent.drones,
            "captain_url": agent.link.base_url or agent.cfg.captain_url,
            "operator": agent.link.user.get("username") if agent.link.user else None,
            "settings_storage": agent.settings_storage,
            "saved_ts": agent.saved_ts,
        }

    @app.post("/api/command")
    @captain_errors
    async def post_command(body: CommandBody):
        need_login()
        if body.action == "custom":
            drone = body.drone_id or agent.target
            if not drone:
                raise HTTPException(422, "No target drone selected")
            if body.custom_command is None:
                raise HTTPException(422, "custom_command ID is required for custom commands")
            import re
            m = re.search(r"(\d+)\s*$", str(drone))
            sys_id = int(m.group(1)) if m else 1
            cmd = {
                "type": "mavlink",
                "droneId": str(drone),
                "sys_id": sys_id,
                "class": "COMMAND_LONG",
                "command": int(body.custom_command),
                "params": {**{f"param{i}": 0 for i in range(1, 8)}, **(body.params or {})},
                "label": body.label or f"Command {body.custom_command} ({drone})",
                "source": "agent",
                "source_hint": "agent",
                "ts": int(time.time() * 1000),
            }
            try:
                res = await agent.link.send_command(cmd)
            except CaptainError as exc:
                agent.hub.emit({"type": "command.result", "ok": False, "label": cmd["label"],
                                "source": "agent", "detail": exc.detail})
                raise HTTPException(exc.status if exc.status in (401, 403, 404, 409, 422) else 502, exc.detail)
            detail = f"{cmd['label']} accepted by Captain"
            agent.hub.emit({"type": "command.result", "ok": True, "label": cmd["label"],
                            "source": "agent", "detail": detail})
            return {"detail": detail, "result": res.get("result")}

        if body.action not in ACTIONS:
            raise HTTPException(422, f"Unknown action: {body.action!r}")
        if body.takeoff_alt_m is not None:
            agent.settings["joystick"]["takeoff_alt_m"] = float(body.takeoff_alt_m)
        return await agent.execute(body.action, body.drone_id, source="agent")

    # ── settings ───────────────────────────────────────────────────────────

    @app.put("/api/settings/{scope}")
    @captain_errors
    async def put_settings(scope: str, body: SettingBody):
        need_login()
        return {"scope": scope, "value": await agent.save_setting(scope, body.value, body.password)}

    @app.post("/api/settings/restore")
    @captain_errors
    async def restore():
        need_login()
        await agent.restore_settings()
        return {"settings": agent.settings, "saved_ts": agent.saved_ts}

    @app.post("/api/settings/{scope}/reset")
    @captain_errors
    async def reset_scope(scope: str):
        need_login()
        if scope not in schema.SCOPES:
            raise HTTPException(404, "unknown scope")
        # Goes through the same checks as any save, so a reset cannot be used to drop an unlock factor.
        await agent.save_setting(scope, schema.merged(scope, None))
        return {"scope": scope, "value": agent.settings[scope]}

    # ── joystick ───────────────────────────────────────────────────────────

    @app.get("/api/joystick")
    async def joystick():
        m = agent.monitor
        return {"info": m.info if m else {},
                "error": m.backend_error if m else None,
                "nav_mode": agent.mapper.nav_mode,
                "pads": [{"id": p.id, "name": p.name, "axes": p.axes,
                        "buttons": p.buttons, "hats": p.hats}
                        for p in (m.pads if m else [])]}


    @app.post("/api/joystick/nav_mode")
    async def post_nav_mode(body: NavModeBody):
        """Toggle joystick hat navigation. Called from the agent UI; the physical
        'nav_toggle' bound button takes the same path through JoystickMapper."""
        await agent.mapper.set_nav_mode(body.on)
        return {"nav_mode": agent.mapper.nav_mode}

    # ── voice ──────────────────────────────────────────────────────────────
    @app.post("/api/voice/assign")
    @captain_errors
    async def voice_assign(body: VoiceAssignBody):
        need_login()
        if body.action not in ACTIONS:
            raise HTTPException(422, f"unknown action {body.action!r}")
        phrase = body.phrase.strip()
        if not phrase:
            raise HTTPException(422, "phrase is required")
        if len(phrase) > 60:
            raise HTTPException(422, "phrase must be 60 characters or fewer")

        stored = await agent.link.my_biometrics()
        vcfg = agent.settings.get("verification", {})

        face_ok = None
        voice_ok = None
        face_score = 0.0
        voice_score = 0.0
        voice_reason = ""

        # ── Face first (cheapest signal, and independent of speech length) ──
        if body.face_jpeg_b64 and stored.get("face"):
            feng = agent.engines["face"]["engine"]
            if feng is not None:
                face_ok, face_score, _ = await run_in_threadpool(
                    verify, feng, _b64(body.face_jpeg_b64), stored["face"],
                    threshold=vcfg.get("face_threshold", feng.threshold),
                )

        # ── Voice, with the threshold lowered if face already passed ──────
        if body.data_b64 and stored.get("voice"):
            veng = agent.engines["voice"]["engine"]
            if veng is not None:
                thresh = (vcfg.get("voice_threshold_with_face", 0.45)
                        if face_ok and vcfg.get("face_assist_voice_commands", True)
                        else vcfg.get("voice_threshold", veng.threshold))
                voice_ok, voice_score, voice_reason = await run_in_threadpool(
                    verify, veng, _b64(body.data_b64), stored["voice"],
                    threshold=thresh,
                )

        # ── Decision matrix ─────────────────────────────────────────────────
        # 1) Strong voice match → accept, no face required
        # 2) Face match + voice weak-but-present → accept (face carries identity)
        # 3) Face match + voice sample too short to score → accept (short phrase)
        # 4) Anything else → reject with the specific failure
        face_carries_identity = bool(face_ok) and vcfg.get("face_assist_voice_commands", True)
        has_voice = bool(stored.get("voice"))

        if has_voice and voice_ok:
            pass                                            # strong voice match
        elif face_carries_identity:
            pass                                            # face carries identity
        elif not has_voice and not stored.get("face"):
            raise HTTPException(409, "no biometric template enrolled")
        elif not has_voice:
            raise HTTPException(422, f"face does not match ({face_score:.3f})")                                       # cases 2 / 3
        else:
            if face_ok is False and voice_ok is False:
                raise HTTPException(422,                    f"neither face ({face_score:.3f}) nor voice ({voice_score:.3f}) matched")
            if voice_ok is False:
                raise HTTPException(422,                    f"voice does not match your enrolled voiceprint: {voice_reason} "
                    f"(score {voice_score:.3f}, threshold {vcfg.get('voice_threshold', 0.75):.2f})")
            if not stored.get("voice") and not stored.get("face"):
                raise HTTPException(409,                    "no biometric template enrolled — ask a Captain admin to enrol face or voice first")
            raise HTTPException(422, "verification failed — no usable sample")

        try:
            if body.dry_run:    
                return {"ok": True, "score": voice_score if voice_ok else face_score, "match": voice_ok or face_ok}
            entry = voice_commands.add_or_update( agent.link.user["username"], phrase, body.action, wake_word=agent.settings.get("voice", {}).get("wake_word", "captain"),)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

        agent.hub.emit({
            "type": "voice.commands.changed",
            "commands": voice_commands.list_user(agent.link.user["username"]),
        })
        return entry

    @app.post("/api/voice/precheck")
    @captain_errors
    async def voice_precheck(body: VoicePrecheckBody):
        need_login()
        stored = await agent.link.my_biometrics()
        has_voice = bool(stored.get("voice"))
        has_face  = bool(stored.get("face"))
        if not has_voice and not has_face:
            raise HTTPException(409, "no biometric template enrolled")

        vcfg = agent.settings.get("verification", {})
        face_ok, face_score = False, 0.0
        if body.face_jpeg_b64 and has_face:
            feng = agent.engines["face"]["engine"]
            if feng is not None:
                face_ok, face_score, _ = await run_in_threadpool(
                    verify, feng, _b64(body.face_jpeg_b64), stored["face"],
                    threshold=vcfg.get("face_threshold", feng.threshold),
                )

        # No voiceprint at all → the face is the only signal. Accept if it matched.
        if not has_voice:
            ok = face_ok and vcfg.get("face_assist_voice_commands", True)
            return {"ok": ok, "voice_ok": None, "voice_score": 0.0,
                    "voice_message": "no voiceprint enrolled — face only",
                    "face_ok": face_ok, "face_score": round(face_score, 3),
                    "threshold": round(vcfg.get("face_threshold", 0.363), 3)}

        # Existing voice-first path (unchanged)
        engine = agent.engines["voice"]["engine"]
        if engine is None:
            raise HTTPException(409, agent.engines["voice"]["reason"] or "voice engine unavailable")
        thresh = (vcfg.get("voice_threshold_with_face", 0.45)
                if face_ok and vcfg.get("face_assist_voice_commands", True)
                else vcfg.get("voice_threshold", engine.threshold))
        voice_ok, voice_score, voice_msg = await run_in_threadpool(
            verify, engine, _b64(body.data_b64), stored["voice"], threshold=thresh,
        )
        return {
            "ok":            voice_ok or face_ok,
            "voice_ok":      voice_ok,
            "voice_score":   round(voice_score, 3),
            "voice_message": voice_msg,
            "face_ok":       face_ok,
            "face_score":    round(face_score, 3),
            "threshold":     round(thresh, 3),
        }

    @app.post("/api/voice/lock")
    async def voice_lock():
        agent.gate.lock()
        log.info("voice gate locked")
        agent.hub.emit({"type": "voice.gate", **agent.gate.snapshot()})
        return agent.gate.snapshot()

    @app.post("/api/voice/unlock")
    @captain_errors
    async def voice_unlock(body: UnlockBody):
        need_login()
        wait = agent.limiter.blocked_for()
        if wait:
            log.warning("voice unlock throttled wait_s=%d", wait)
            raise HTTPException(429, f"Too many failed attempts — try again in {wait} s")
        required = agent.settings["voice"]["unlock_factors"]
        results, stored, any_failed = {}, None, False
        for factor in required:
            if factor == "password":
                ok = bool(body.password) and await agent.link.verify_password(body.password)
                results[factor] = {"ok": ok, "message": "" if ok else "wrong password"}
                any_failed |= not ok
                continue
            kind, b64 = ("voice", body.voice_wav_b64) if factor == "voiceprint" else ("face", body.face_jpeg_b64)
            engine = agent.engines[kind]["engine"]
            if engine is None:
                results[factor] = {"ok": False, "message": agent.engines[kind]["reason"] or "engine unavailable"}
            elif not b64:
                results[factor] = {"ok": False, "message": "no sample provided"}
            else:
                stored = stored if stored is not None else await agent.link.my_biometrics()
                ok, score, msg = await run_in_threadpool(verify, engine, _b64(b64), stored.get(kind))
                results[factor] = {"ok": ok, "message": msg, "score": round(score, 3)}
                any_failed |= not ok
        unlocked = all(r["ok"] for r in results.values())
        if unlocked:
            agent.limiter.reset()
            agent.gate.unlock(required)
            log.info("voice unlock succeeded factors=%s", required)
            agent.hub.emit({"type": "voice.gate", **agent.gate.snapshot()})
        elif any_failed:
            agent.limiter.fail()
            log.warning("voice unlock failed factors=%s",
                        {factor: result["ok"] for factor, result in results.items()})
        return {"unlocked": unlocked, "factors": results, "gate": agent.gate.snapshot()}

    # ── voice commands (local registry) ────────────────────────────────────
    @app.post("/api/voice/transcript")
    @captain_errors
    async def transcript(body: TranscriptBody):
        need_login()
        text = body.text[:300]
        log.info("voice transcript received (final=%s): %r", body.final, text)
        if not body.final:                       # interim text is never traced and never acted on
            return (await agent.voice.handle(text, final=False)).as_dict()

        engine = (body.engine or agent.settings["voice"]["stt_engine"])[:32]
        # A spoken "confirm"/"cancel" continues the trace of the sentence it answers.
        continuing = agent.voice.pending is not None and agent.pending_trace is not None
        tid = agent.pending_trace if continuing else new_trace("vc")
        agent._trace_begin(tid, text, engine)

        token = TRACE.set(tid)                   # Agent.execute() reads this
        try:
            res = await agent.voice.handle(text, final=True)
        finally:
            TRACE.reset(token)
        agent._trace(tid, "verdict", status=res.status, action=res.action,
                     drone=res.drone_id, detail=res.detail)
        if res.status == "needs_confirm":
            agent.pending_trace = tid
        elif agent.voice.pending is None:
            agent.pending_trace = None

        log.info("voice transcript result: trace=%s status=%s action=%s drone_id=%s detail=%r",
                 tid, res.status, res.action or "-", res.drone_id or "-", res.detail)
        agent.hub.emit({"type": "voice.heard", **res.as_dict(), "trace": tid})
        return {**res.as_dict(), "trace": tid}

    @app.post("/api/voice/confirm")
    @captain_errors
    async def voice_confirm(body: ConfirmBody):
        need_login()
        tid = agent.pending_trace or new_trace("vc")
        agent.pending_trace = None
        agent._trace_begin(tid, "(confirm button)" if body.ok else "(cancel button)", "button", count=False)
        token = TRACE.set(tid)
        try:
            res = await agent.voice.confirm(body.ok, then_action=body.then_action)
        finally:
            TRACE.reset(token)
        agent._trace(tid, "verdict", status=res.status, action=res.action,
                     drone=res.drone_id, detail=res.detail)
        agent.hub.emit({"type": "voice.heard", **res.as_dict(), "trace": tid})
        return {**res.as_dict(), "trace": tid}

    @app.post("/api/voice/stt_event")
    async def voice_stt_event(body: SttEventBody):
        """The browser reports its speech engine starting, stopping or failing — the only
        way the agent can tell 'nothing was said' from 'the engine is dead'."""
        need_login()
        try:
            ev = agent.traces.stt_event(body.kind, body.detail, body.engine)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if ev is not None:                       # None = repeat of the same error inside 5 s
            agent.hub.emit({"type": "voice.stt", **ev})
        return {"ok": True}

    @app.get("/api/voice/trace")
    async def get_voice_trace(limit: int = 30):
        need_login()
        return agent.traces.snapshot(max(1, min(limit, 100)))


    
    @app.get("/api/voice/commands")
    async def list_voice_commands():
        need_login()
        return {"commands": voice_commands.list_user(agent.link.user["username"])}

    @app.post("/api/voice/commands")
    @captain_errors
    async def reg_voice_command(body: VoiceCommandBody):
        need_login()
        if body.action not in ACTIONS:
            raise HTTPException(422, f"unknown action {body.action!r}")
        if not body.phrase.strip():
            raise HTTPException(422, "phrase is required")

        # Optional voice verification against the enrolled template
        if body.voice_wav_b64:
            engine = agent.engines["voice"]["engine"]
            if engine is not None:
                stored = await agent.link.my_biometrics()
                if stored.get("voice"):
                    ok, score, msg = await run_in_threadpool(
                        verify, engine, _b64(body.voice_wav_b64), stored["voice"]
                    )
                    if not ok:
                        raise HTTPException(
                            422,
                            f"voice sample does not match your enrolled voiceprint: {msg}",
                        )

        try:
            entry = voice_commands.add(
                agent.link.user["username"], body.phrase, body.action
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc))

        agent.hub.emit({
            "type": "voice.commands.changed",
            "commands": voice_commands.list_user(agent.link.user["username"]),
        })
        log.info("voice command added action=%s phrase=%r", body.action, body.phrase.strip())
        return entry

    @app.delete("/api/voice/commands/{entry_id}")
    async def delete_voice_command(entry_id: str):
        need_login()
        if not voice_commands.remove(agent.link.user["username"], entry_id):
            raise HTTPException(404, "voice command not found")
        log.info("voice command removed id=%s", entry_id)
        agent.hub.emit({
            "type": "voice.commands.changed",
            "commands": voice_commands.list_user(agent.link.user["username"]),
        })
        return {"deleted": entry_id}
    
    @app.put("/api/voice/commands/{entry_id}")
    @captain_errors
    async def update_voice_command(entry_id: str, body: VoiceCommandBody):
        need_login()
        if body.action not in ACTIONS:
            raise HTTPException(422, f"unknown action {body.action!r}")
        if not body.phrase.strip():
            raise HTTPException(422, "phrase is required")

        # Optional voice verification (same policy as create)
        if body.voice_wav_b64:
            engine = agent.engines["voice"]["engine"]
            if engine is not None:
                stored = await agent.link.my_biometrics()
                if stored.get("voice"):
                    ok, score, msg = await run_in_threadpool(
                        verify, engine, _b64(body.voice_wav_b64), stored["voice"]
                    )
                    if not ok:
                        raise HTTPException(
                            422,
                            f"voice sample does not match your enrolled voiceprint: {msg}",
                        )

        try:
            entry = voice_commands.update(
                agent.link.user["username"], entry_id, body.phrase, body.action
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc))

        agent.hub.emit({
            "type": "voice.commands.changed",
            "commands": voice_commands.list_user(agent.link.user["username"]),
        })
        log.info("voice command updated id=%s action=%s phrase=%r",
                 entry_id, body.action, body.phrase.strip())
        return entry



    # ── register (admin) ───────────────────────────────────────────────────

    @app.get("/api/register/users")
    @captain_errors
    async def reg_users():
        need_admin()
        users = await agent.link.list_users_with_biometrics()
        mapped = [
            {
                "id":         u.get("id"),
                "username":   u["username"],
                "role":       u["role"],
                "active":     u.get("active", True),
                "biometrics": u.get("biometrics", {"voice": False, "face": False}),
            }
            for u in users
        ]
        log.info("GET /api/register/users -> %d user(s)  names=%s",
                 len(mapped), [m["username"] for m in mapped][:5])
        return mapped

    @app.post("/api/register/preview")
    @captain_errors
    async def reg_preview(body: PreviewBody):
        """Live face detection for the enrol UI. No storage, no embedding —
        just 'is a single correctly-sized face in this frame right now?'"""
        need_admin()
        engine = agent.engines["face"]["engine"]
        if engine is None:
            raise HTTPException(409, agent.engines["face"]["reason"] or "face engine unavailable")
        detect = getattr(engine, "detect", None)
        if detect is None:
            raise HTTPException(409, "face engine does not support preview")
        result = await run_in_threadpool(detect, _b64(body.data_b64))
        return result

    @app.patch("/api/register/user/{username}")
    @captain_errors
    async def update_register_user(username: str, body: UpdateUserBody):
        need_admin()
        users = await agent.link.list_users_with_biometrics()
        target = next((u for u in users if u["username"] == username), None)
        if target is None:
            raise HTTPException(404, f"No such user: {username!r}")
        if not body.password and not body.role:
            raise HTTPException(422, "no changes requested")
        # Same last-active-admin guard Captain enforces; this catches it earlier
        # with a clearer message before the round-trip.
        if (body.role and body.role != "admin"
                and target.get("role") == "admin" and target.get("active")):
            active_admins = sum(1 for u in users
                                if u.get("role") == "admin" and u.get("active"))
            if active_admins <= 1:
                raise HTTPException(409, "Cannot demote the last active admin account")
        result = await agent.link.update_user(
            target["id"], password=body.password, role=body.role,
        )
        agent.hub.emit({"type": "register.users.changed"})
        return {"username": username, "role": result.get("role", target["role"])}

    @app.delete("/api/register/user/{username}")
    @captain_errors
    async def delete_register_user(username: str):
        need_admin()
        users = await agent.link.list_users_with_biometrics()
        target = next((u for u in users if u["username"] == username), None)
        if target is None:
            raise HTTPException(404, f"No such user: {username!r}")
        if agent.link.user and agent.link.user["username"] == username:
            raise HTTPException(409, "You cannot delete your own account")
        if target.get("role") == "admin" and target.get("active"):
            active_admins = sum(1 for u in users
                                if u.get("role") == "admin" and u.get("active"))
            if active_admins <= 1:
                raise HTTPException(409, "Cannot delete the last active admin account")
        await agent.link.delete_user(target["id"])
        agent.hub.emit({"type": "register.users.changed"})
        return {"ok": True, "username": username}

    @app.post("/api/register/user")
    @captain_errors
    async def reg_user(body: NewUserBody):
        need_admin()
        u = await agent.link.create_user(body.username.strip(), body.password, body.role)
        return {"username": u["username"], "role": u["role"]}

    @app.post("/api/register/start")
    @captain_errors
    async def reg_start(body: EnrolBody):
        need_admin()
        if body.kind not in ("face", "voice"):
            raise HTTPException(422, "kind must be 'face' or 'voice'")
        engine = agent.engines[body.kind]["engine"]
        if engine is None:
            raise HTTPException(409, agent.engines[body.kind]["reason"] or "engine unavailable")
        if body.username not in {u["username"] for u in await agent.link.list_users()}:
            raise HTTPException(404, f"No such Captain user: {body.username!r}")
        e = agent.enrollments.start(username=body.username, kind=body.kind, admin=agent.link.user["username"],
                                    needed=engine.needed_samples, consistency_floor=engine.consistency_floor)
        return {"session_id": e.session_id, "needed": e.needed, "count": 0}

    @app.post("/api/register/sample")
    @captain_errors
    async def reg_sample(body: SampleBody):
        need_admin()
        e = agent.enrollments.get(body.session_id, agent.link.user["username"])
        engine = agent.engines[e.kind]["engine"]
        vec = await run_in_threadpool(engine.embed, _b64(body.data_b64))
        e.add(vec)
        return {"count": len(e.vectors), "needed": e.needed, "ready": e.ready}

    @app.post("/api/register/commit")
    @captain_errors
    async def reg_commit(body: SessionBody):
        need_admin()
        e = agent.enrollments.get(body.session_id, agent.link.user["username"])
        engine = agent.engines[e.kind]["engine"]
        await agent.link.put_biometric(e.username, e.kind, engine.model_id, e.template())
        agent.enrollments.drop(body.session_id)
        return {"username": e.username, "kind": e.kind, "enrolled": True}

    @app.post("/api/register/revoke")
    @captain_errors
    async def reg_revoke(body: EnrolBody):
        need_admin()
        await agent.link.delete_biometric(body.username, body.kind)
        return {"username": body.username, "kind": body.kind, "revoked": True}

    # ── live events ────────────────────────────────────────────────────────

    @app.websocket("/ws/ui")
    async def ws_ui(ws: WebSocket):
        why = guard.check(host=ws.headers.get("host"), origin=ws.headers.get("origin"),
                          cookie_token=ws.cookies.get(COOKIE), method="GET")
        if why:
            await ws.close(code=4403)
            return
        await ws.accept()
        q, watching = agent.hub.subscribe(), False
        await ws.send_json({"type": "status", **agent.status()})

        async def sender():
            while True:
                await ws.send_json(await q.get())

        async def receiver():
            nonlocal watching
            while True:
                msg = await ws.receive_json()
                want = bool(isinstance(msg, dict) and msg.get("watch") == "joystick" and msg.get("on"))
                if want != watching:
                    agent.pad_watchers += 1 if want else -1
                    watching = want

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except WebSocketDisconnect:
            pass
        finally:
            for t in tasks:
                t.cancel()
            if watching:
                agent.pad_watchers -= 1
            agent.hub.unsubscribe(q)

    return app, agent










if __name__ == "__main__":
    import threading
    import webbrowser
    import uvicorn
    cfg = Config.build(port=None, captain=None, open_browser=True)
    app, agent = create_app(cfg)
    url = agent.guard.launch_url()
    print(f"\nFleet Agent is running. Open this link:\n\n    {url}\n")
    if cfg.open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=cfg.port, log_level="warning")
