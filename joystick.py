"""
fleet_agent/joystick.py
Joystick detection and button mapping.

Reading happens in Python (pygame/SDL2) rather than the browser's Gamepad API
on purpose: browsers stop delivering gamepad data to a tab that is in the
background, and the operator will normally be looking at the Captain map tab.

What the joystick does in this version: each button can be bound to one of the
discrete, allowlisted Captain commands (commands.ACTIONS). Analog sticks are
read and drawn, but they do NOT fly the drone: Captain's command_parser has no
manual-control path and ALLOWED_MAVLINK_COMMANDS deliberately excludes one.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Protocol

from .commands import ACTIONS, PSEUDO_ACTIONS

log = logging.getLogger(__name__)


@dataclass
class PadState:
    id: int
    name: str
    axes: list[float] = field(default_factory=list)
    buttons: list[bool] = field(default_factory=list)
    hats: list[tuple[int, int]] = field(default_factory=list)


class Backend(Protocol):
    def poll(self) -> list[PadState]: ...
    def close(self) -> None: ...


class PygameBackend:
    """SDL2 joystick access with hot-plug. Headless: no window is opened."""

    def __init__(self) -> None:
        # Without these, SDL on some platforms only reports joystick input while one of
        # its own windows has focus — and this process has no window.
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        import pygame
        self._pg = pygame
        pygame.display.init()
        pygame.joystick.init()
        self._pads: dict[int, "pygame.joystick.JoystickType"] = {}
        for i in range(pygame.joystick.get_count()):
            self._add(i)

    def _add(self, device_index: int) -> None:
        j = self._pg.joystick.Joystick(device_index)
        self._pads[j.get_instance_id()] = j

    def poll(self) -> list[PadState]:
        pg = self._pg
        for e in pg.event.get():
            if e.type == pg.JOYDEVICEADDED:
                try:
                    self._add(e.device_index)
                except Exception as exc:                       # pad vanished mid-enumeration
                    log.debug("joystick add failed: %s", exc)
            elif e.type == pg.JOYDEVICEREMOVED:
                self._pads.pop(e.instance_id, None)
        out = []
        for iid, j in self._pads.items():
            out.append(PadState(
                id=iid, name=j.get_name(),
                axes=[round(j.get_axis(i), 3) for i in range(j.get_numaxes())],
                buttons=[bool(j.get_button(i)) for i in range(j.get_numbuttons())],
                hats=[tuple(j.get_hat(i)) for i in range(j.get_numhats())]))
        return out

    def close(self) -> None:
        self._pg.joystick.quit()
        self._pg.display.quit()


class JoystickMonitor:
    """Polls a backend on its own thread; reports presence changes and button
    edges back onto the asyncio loop."""

    AXIS_THRESHOLD = 0.05

    def __init__(self, backend_factory, loop, *,
                 on_presence, on_edge, on_hat, on_axes,
                 poll_hz: float = 50.0) -> None:
        self._factory, self._loop = backend_factory, loop
        self._on_presence = on_presence
        self._on_edge     = on_edge
        self._on_hat      = on_hat
        self._on_axes     = on_axes
        self._period      = 1.0 / poll_hz
        self._stop        = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock        = threading.Lock()
        self._pads: list[PadState] = []
        self.backend_error: Optional[str] = None
        self._info = {"present": False, "name": "", "count": 0}

    def _run(self) -> None:
        try:
            backend = self._factory()
        except Exception as exc:
            self.backend_error = f"{type(exc).__name__}: {exc}"
            log.warning("joystick support unavailable: %s", self.backend_error)
            return
        prev: dict[int, list[bool]] = {}
        prev_hats: dict[int, list[tuple[int, int]]] = {}
        prev_axes: dict[int, list[float]] = {}
        try:
            while not self._stop.is_set():
                pads = backend.poll()
                info = {"present": bool(pads), "name": pads[0].name if pads else "", "count": len(pads)}
                with self._lock:
                    self._pads = pads
                    changed = info != self._info
                    self._info = info
                if changed:
                    self._loop.call_soon_threadsafe(self._on_presence, dict(info))
                seen = set()
                for p in pads:
                    seen.add(p.id)
                    # ── buttons ─────────────────────────────────────
                    old = prev.get(p.id, [False] * len(p.buttons))
                    for i, now in enumerate(p.buttons):
                        was = old[i] if i < len(old) else False
                        if now != was:
                            asyncio.run_coroutine_threadsafe(self._on_edge(p.id, i, now), self._loop)
                    prev[p.id] = p.buttons
                    # ── hats ────────────────────────────────────────
                    old_h = prev_hats.get(p.id, [(0, 0)] * len(p.hats))
                    for h, (hx, hy) in enumerate(p.hats):
                        ohx, ohy = old_h[h] if h < len(old_h) else (0, 0)
                        if (hx, hy) != (ohx, ohy):
                            asyncio.run_coroutine_threadsafe(
                                self._on_hat(p.id, h, int(hx), int(hy)), self._loop)
                    prev_hats[p.id] = list(p.hats)
                    # ── axes (significant change only) ──────────────
                    old_a = prev_axes.get(p.id, [0.0] * len(p.axes))
                    moved = any(abs(n - (old_a[i] if i < len(old_a) else 0.0)) > self.AXIS_THRESHOLD
                                for i, n in enumerate(p.axes))
                    if moved:
                        asyncio.run_coroutine_threadsafe(
                            self._on_axes(p.id, list(p.axes)), self._loop)
                    prev_axes[p.id] = list(p.axes)

                for gone in set(prev) - seen:      del prev[gone]
                for gone in set(prev_hats) - seen: del prev_hats[gone]
                for gone in set(prev_axes) - seen: del prev_axes[gone]
                time.sleep(self._period)
        finally:
            try: backend.close()
            except Exception: pass

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="joystick", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    @property
    def info(self) -> dict:
        with self._lock:
            return dict(self._info)

    @property
    def pads(self) -> list[PadState]:
        with self._lock:
            return list(self._pads)


class JoystickMapper:
    REPEAT_GUARD_S     = 0.4
    HAT_REPEAT_GUARD_S = 0.15
    AXIS_THROTTLE_S    = 1.0 / 15.0          # ~15 Hz axis streaming
    AXIS_DEADBAND      = 0.15

    def __init__(self, *, settings, present, dispatch, emit, link=None) -> None:
        self._settings, self._present, self._dispatch, self._emit = settings, present, dispatch, emit
        self._link = link
        self._on_voice_toggle = on_voice_toggle
        self._holds: dict[tuple[int, int], asyncio.Task] = {}
        self._last_fire: dict[tuple[int, int], float] = {}
        self._last_hat: dict[tuple[int, int], float] = {}
        self._last_axis_send: float = 0.0
        self._nav_mode: bool = True

    # ── button edges ───────────────────────────────────────────────
    async def on_edge(self, pad_id: int, index: int, pressed: bool) -> None:
        key = (pad_id, index)
        self._emit({"type": "joystick.button", "pad": pad_id, "index": index, "pressed": pressed})
        if not pressed:
            task = self._holds.pop(key, None)
            if task and not task.done():
                task.cancel()
                self._emit({"type": "joystick.hold", "index": index, "state": "cancelled"})
            return

        cfg    = self._settings()
        action = cfg.get("bindings", {}).get(str(index))

        # nav_toggle — flips local nav mode
        if action == "nav_toggle":
            await self.set_nav_mode(not self._nav_mode)
            return

        # voice_lock_toggle — LOCAL action, does not go to Captain
        if action == "voice_lock_toggle":
            if self._on_voice_toggle is not None:
                try:
                    result = await self._on_voice_toggle()
                    self._emit({"type": "voice.gate.toggle", **result})
                except Exception as exc:
                    self._emit({"type": "joystick.error",
                                "action": action, "detail": str(exc)})
            return

        # UI pseudo-actions route via nav channel (only when nav mode is on)
        if action in PSEUDO_ACTIONS:
            if not self._nav_mode:
                return
            if time.monotonic() - self._last_fire.get(key, 0.0) < self.REPEAT_GUARD_S:
                return
            self._last_fire[key] = time.monotonic()
            if self._link:
                self._link.send_nav(action)
            self._emit({"type": "joystick.nav_fired", "action": action, "index": index})
            return

        # ACTIONS — real drone commands
        if not action or action not in ACTIONS or not self._present():
            return
        if time.monotonic() - self._last_fire.get(key, 0.0) < self.REPEAT_GUARD_S:
            return
        if ACTIONS[action].confirm:
            self._holds[key] = asyncio.create_task(
                self._hold_then_fire(key, index, action, cfg["hold_ms"]))
        else:
            await self._fire(key, action)

    # ── hat edges ──────────────────────────────────────────────────
    async def on_hat(self, pad_id: int, hat_index: int, x: int, y: int) -> None:
        self._emit({"type": "joystick.hat", "pad": pad_id, "index": hat_index, "x": x, "y": y})
        if (x, y) == (0, 0) or not self._present():
            return

        now = time.monotonic()
        dk  = (pad_id, hat_index)
        if now - self._last_hat.get(dk, 0.0) < self.HAT_REPEAT_GUARD_S:
            return
        self._last_hat[dk] = now

        cfg = self._settings()

        # nav mode off — hat plays hat_bindings (real actions only)
        if not self._nav_mode:
            bound = (cfg.get("hat_bindings") or {}).get(f"{hat_index}:{x},{y}")
            if bound and bound in ACTIONS:
                await self._dispatch(bound)
            return

        # nav mode on — hat pans the map
        if not self._link:
            return
        if   y >  0: action = "pan_up"      # pygame hat +Y = up
        elif y <  0: action = "pan_down"
        elif x <  0: action = "pan_left"
        elif x >  0: action = "pan_right"
        else: return
        self._link.send_nav(action)

    # ── axis streaming ─────────────────────────────────────────────
    async def on_axes(self, pad_id: int, axes: list[float]) -> None:
        if not self._present() or not self._link:   return
        if not self._nav_mode:                      return   # ← new: only stream while armed for nav
        now = time.monotonic()
        if now - self._last_axis_send < self.AXIS_THROTTLE_S:
            return
        self._last_axis_send = now

        cfg      = self._settings()
        deadband = float(cfg.get("deadzone", 0.15))
        a0 = axes[0] if len(axes) > 0 else 0.0
        a1 = axes[1] if len(axes) > 1 else 0.0
        a2 = axes[2] if len(axes) > 2 else 0.0
        a3 = axes[3] if len(axes) > 3 else 0.0

        def dz(v: float) -> float:
            return 0.0 if abs(v) < deadband else float(v)

        x = dz(a0); y = dz(a1); rx = dz(a2); ry = dz(a3)
        if x == 0 and y == 0 and rx == 0 and ry == 0:
            return

        # Captain decides what to do: pan the map (nothing selected) or manual
        # flight control (drone selected and armed). Agent just streams raw.
        self._link.send_nav("axes", x=x, y=y, rx=rx, ry=ry)

    # ── nav-mode toggle ────────────────────────────────────────────
    @property
    def nav_mode(self) -> bool:
        return self._nav_mode

    async def set_nav_mode(self, on: bool) -> None:
        on = bool(on)
        if self._nav_mode == on:
            return
        self._nav_mode = on
        self._emit({"type": "joystick.nav_mode", "on": on})
        if self._link:
            self._link.notify_state_changed()

    async def _hold_then_fire(self, key, index: int, action: str, hold_ms: int) -> None:
        self._emit({"type": "joystick.hold", "index": index, "action": action,
                    "ms": hold_ms, "state": "start"})
        await asyncio.sleep(hold_ms / 1000)
        self._holds.pop(key, None)
        if not self._present():
            self._emit({"type": "joystick.hold", "index": index, "state": "cancelled"})
            return
        self._emit({"type": "joystick.hold", "index": index, "action": action, "state": "fired"})
        await self._fire(key, action)

    async def _fire(self, key, action: str) -> None:
        self._last_fire[key] = time.monotonic()
        try:
            await self._dispatch(action)
        except Exception as exc:
            self._emit({"type": "joystick.error", "action": action, "detail": str(exc)})

