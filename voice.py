"""
fleet_agent/voice.py
Voice control logic, independent of any speech engine. Transcripts arrive from
whatever engine the browser (or a future server-side engine) runs; everything
that decides whether a sentence may fly a drone lives here.

Layers a spoken command passes, in order:
  1. VoiceGate     — voice control stays locked until the operator unlocks it
                     (Captain password, and optionally voiceprint/face) and
                     re-locks after unlock_ttl_s without a command.
  2. wake word     — by default a command must start with "captain" so room
                     chatter containing "land" or "stop" does nothing.
  3. exact grammar — the rest of the sentence must be exactly one known phrase
                     (+ optional "drone <id>"). No fuzzy matching.
  4. confirmation  — arm / disarm / takeoff are held until the operator says
                     "confirm" (or presses Confirm) inside confirm_window_s.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, asdict
from typing import Awaitable, Callable, Optional

from .commands import ACTIONS

_VERBS: dict[str, tuple[str, ...]] = {
    "arm":     ("arm", "arm motors"),
    "disarm":  ("disarm", "disarm motors"),
    "takeoff": ("takeoff", "take off", "launch"),
    "land":    ("land", "landing"),
    "rtl":     ("return to launch", "return home", "return to home", "rtl", "come home"),
    "hold":    ("hold", "hold position", "loiter", "stay", "stop"),
    "pause":   ("pause", "pause mission"),
    "resume":  ("resume", "resume mission", "continue mission"),
}
_PHRASE_TO_ACTION = {p: a for a, ps in _VERBS.items() for p in ps}
_FILLER = {"please", "now", "the", "a", "motors"}
# Tokens that confirm a pending prompt
_CONFIRM = {"confirm", "yes confirm", "confirmed", "execute",
            "affirmative", "do it", "proceed", "go ahead", "yes"}
# Tokens that cancel a pending prompt
_CANCEL  = {"cancel", "abort", "negative", "no", "never mind", "nevermind"}
# Escape-hatch tokens: cancel pending AND fire the corresponding action
_HOLD_LIKE = {
    "hold":  "hold",    # → loiter
    "hold position": "hold",
    "stop":  "hold",
    "halt":  "hold",
    "wait":  "hold",
    "stay":  "hold",
    "pause": "pause",
    "pause mission": "pause",
}
_DIGIT_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "to": "2", "too": "2",
                "three": "3", "four": "4", "for": "4", "five": "5", "six": "6",
                "seven": "7", "eight": "8", "nine": "9"}


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())).strip()


@dataclass
class VoiceResult:
    status: str     # ignored | locked | needs_confirm | executed | rejected | cancelled | expired
    text: str
    action: Optional[str] = None
    drone_id: Optional[str] = None
    detail: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class VoiceGate:
    def __init__(self, ttl_s: int = 300, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.ttl_s = ttl_s
        self._until = 0.0
        self.unlocked_by: list[str] = []
        self._last_unlock_at: float = 0.0     # when factors last verified

    @property
    def unlocked(self) -> bool:
        return self._clock() < self._until

    @property
    def remaining_s(self) -> int:
        return max(0, int(self._until - self._clock())) if self.unlocked else 0

    def unlock(self, factors: list[str]) -> None:
        self._until = self._clock() + self.ttl_s
        self._last_unlock_at = self._clock()
        self.unlocked_by = list(factors)

    def touch(self) -> None:
        if self.unlocked:
            self._until = self._clock() + self.ttl_s

    def lock(self) -> None:
        self._until = 0.0
        self.unlocked_by = []

    def quick_reenable(self, grace_s: float = 90.0) -> bool:
        """Re-enable without re-authenticating, if a real unlock happened recently."""
        if self._last_unlock_at == 0.0:
            return False
        if self._clock() - self._last_unlock_at > grace_s:
            return False
        self._until = self._clock() + self.ttl_s
        return True

    def toggle(self, grace_s: float = 90.0) -> dict:
        """Physical button toggle. Returns the new snapshot + reason."""
        if self.unlocked:
            self.lock()
            return {**self.snapshot(), "changed": True, "reason": "locked by button"}
        if self.quick_reenable(grace_s):
            return {**self.snapshot(), "changed": True, "reason": "re-enabled by button"}
        return {**self.snapshot(), "changed": False,
                "reason": "locked — re-unlock via password / voice / face first"}

    def snapshot(self) -> dict:
        return {"unlocked": self.unlocked, "remaining_s": self.remaining_s,
                "ttl_s": self.ttl_s,
                "unlocked_by": list(self.unlocked_by) if self.unlocked else []}




def _parse_target(tokens: list[str]) -> tuple[Optional[str], list[str]]:
    """Pull 'drone <id>' out of the token list. Digit-words only count here, so
    'return to launch' is never turned into 'return 2 launch'."""
    for i, tok in enumerate(tokens):
        if tok != "drone" or i + 1 >= len(tokens):
            continue
        j, digits = i + 1, ""
        while j < len(tokens) and (tokens[j].isdigit() or tokens[j] in _DIGIT_WORDS):
            digits += tokens[j] if tokens[j].isdigit() else _DIGIT_WORDS[tokens[j]]
            j += 1
        if digits:
            return digits, tokens[:i] + tokens[j:]
        return tokens[i + 1], tokens[:i] + tokens[i + 2:]
    return None, tokens


class VoiceController:

    def __init__(self, gate: VoiceGate, *,
                 settings: Callable[[], dict],
                 known_drones: Callable[[], list[str]],
                 default_drone: Callable[[], Optional[str]],
                 execute: Callable[[str, str, str], Awaitable[dict]],
                 local_matcher: Optional[Callable[[str], Optional[dict]]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.gate, self._settings, self._known = gate, settings, known_drones
        self._default, self._execute, self._clock = default_drone, execute, clock
        self._local_match = local_matcher
        self._pending: Optional[tuple[str, str, str, float]] = None

    @property
    def pending(self) -> Optional[dict]:
        if self._pending and self._clock() < self._pending[3]:
            a, d, _t, dl = self._pending
            return {"action": a, "drone_id": d, "expires_in_s": max(0, int(dl - self._clock()))}
        return None

    def _wake_ok(self, tokens: list[str]) -> tuple[bool, list[str]]:
        cfg = self._settings()
        if not cfg.get("require_wake_word", True):
            return True, tokens
        wake = normalize(cfg.get("wake_word", "captain"))
        aliases = {"captain", "capitan"} if wake in ("captain", "capitan") else {wake}
        if tokens[:1] == ["hey"]:
            tokens = tokens[1:]
        for alias in aliases:
            w = alias.split()
            if tokens[:len(w)] == w:
                return True, tokens[len(w):]
        return False, tokens

    def _parse(self, text: str) -> tuple[Optional[str], Optional[str], bool]:
        """(action, spoken_drone_id, wake_ok). action None = not a command."""
        tokens = normalize(text).split()
        ok, tokens = self._wake_ok(tokens)
        drone, tokens = _parse_target(tokens)
        phrase = " ".join(t for t in tokens if t not in _FILLER)
        return _PHRASE_TO_ACTION.get(phrase), drone, ok

    def _match_custom(self, text: str) -> Optional[tuple[str, Optional[str], str]]:
        """(action, drone_from_phrase_or_None, matched_phrase) or None."""
        if not self._local_match:
            return None
        try:
            entry = self._local_match(text)
        except Exception:
            return None
        if entry:
            return (entry["action"], None, entry["phrase"])

        # Try stripping a trailing "drone N" so "arm the drone 2" matches phrase "arm the drone"
        tokens = text.split()
        if len(tokens) >= 3 and tokens[-2] == "drone":
            last = tokens[-1]
            drone = last if last.isdigit() else _DIGIT_WORDS.get(last)
            if drone:
                stripped = " ".join(tokens[:-2])
                try:
                    entry = self._local_match(stripped)
                except Exception:
                    entry = None
                if entry:
                    return (entry["action"], drone, entry["phrase"])
        return None

    async def handle(self, text: str, *, final: bool = True) -> VoiceResult:
        if not final or not normalize(text):
            return VoiceResult("ignored", text)
        norm = normalize(text)

        # Pending-confirm window must expire cleanly
        if self._pending and self._clock() >= self._pending[3]:
            self._pending = None
            if norm in _CONFIRM:
                return VoiceResult("expired", text,
                                detail="confirmation window passed — say the command again")

        # ── Conversational responses take priority over normal grammar ──
        if self._pending:
            if norm in _CONFIRM:
                return await self.confirm(True, text)
            if norm in _CANCEL:
                return await self.confirm(False, text)
            if norm in _HOLD_LIKE:
                # Reject the pending action AND fire the escape-hatch action.
                self._pending = None
                action = _HOLD_LIKE[norm]
                drone = self._resolve_drone(None)
                if isinstance(drone, VoiceResult):
                    drone.text = text; drone.action = action
                    return drone
                return await self._run(action, drone, text)
            # Any other utterance during a pending prompt is ignored — do NOT let
            # random speech confirm a motor command.
            return VoiceResult("ignored", text,
                            detail=f"waiting for confirm / cancel / hold "
                                    f"({int(self._pending[3] - self._clock())} s left)")

        # ── No pending prompt: existing custom → grammar flow unchanged ──
        tokens = norm.split()
        wake_ok, tokens_after_wake = self._wake_ok(tokens)
        if wake_ok:
            text_after_wake = " ".join(tokens_after_wake)
            custom = self._match_custom(text_after_wake)
            if custom:
                action, drone_from_phrase, phrase = custom
                if not self.gate.unlocked:
                    return VoiceResult("locked", text, action, drone_from_phrase,
                                    "voice control is locked — unlock it first")
                drone = drone_from_phrase or self._resolve_drone(None)
                if isinstance(drone, VoiceResult):
                    drone.text = text; drone.action = action
                    return drone
                if ACTIONS[action].confirm:
                    window = int(self._settings().get("confirm_window_s", 8))
                    self._pending = (action, drone, text, self._clock() + window)
                    return VoiceResult("needs_confirm", text, action, drone,
                                    f"say 'confirm' within {window} s to "
                                    f"{ACTIONS[action].label.lower()} drone {drone}")
                return await self._run(action, drone, text)

        # ── Built-in grammar (unchanged) ──
        action, spoken, wake_ok2 = self._parse(text)
        if action is None or not wake_ok2:
            return VoiceResult("ignored", text)
        if not self.gate.unlocked:
            return VoiceResult("locked", text, action, spoken, "voice control is locked — unlock it first")
        drone = self._resolve_drone(spoken)
        if isinstance(drone, VoiceResult):
            drone.text, drone.action = text, action
            return drone
        self._pending = None
        if ACTIONS[action].confirm:
            window = int(self._settings().get("confirm_window_s", 8))
            self._pending = (action, drone, text, self._clock() + window)
            return VoiceResult("needs_confirm", text, action, drone,
                               f"say 'confirm' within {window} s to "
                               f"{ACTIONS[action].label.lower()} drone {drone}")
        return await self._run(action, drone, text)

    def _resolve_drone(self, spoken: Optional[str]):
        known = self._known()
        if spoken is None:
            default = self._default()
            if not default:
                return VoiceResult("rejected", "", detail="no target drone — pick one in the agent or say 'drone <id>'")
            return default
        by_lower = {k.lower(): k for k in known}
        if known and spoken.lower() not in by_lower:
            return VoiceResult("rejected", "", drone_id=spoken, detail=f"unknown drone {spoken!r}")
        return by_lower.get(spoken.lower(), spoken)

    async def confirm(self, ok: bool, text: str = "", then_action: Optional[str] = None) -> VoiceResult:
        pend, self._pending = self._pending, None
        if pend is None or self._clock() >= pend[3]:
            return VoiceResult("expired", text, detail="nothing waiting for confirmation")
        action, drone, original, _ = pend
        if not ok:
            if then_action in ACTIONS:
                if not self.gate.unlocked:
                    return VoiceResult("locked", text or original, then_action, drone, "voice control locked while waiting")
                return await self._run(then_action, drone, text or original)
            return VoiceResult("cancelled", text or original, action, drone, "cancelled")
        if not self.gate.unlocked:
            return VoiceResult("locked", text or original, action, drone, "voice control locked while waiting")
        return await self._run(action, drone, text or original)


    async def _run(self, action: str, drone: str, text: str) -> VoiceResult:
        try:
            result = await self._execute(action, drone, "voice")
        except Exception as exc:                       # Captain refusal, network, no session ...
            return VoiceResult("rejected", text, action, drone, str(exc))
        self.gate.touch()
        return VoiceResult("executed", text, action, drone, str(result.get("detail", "")) if isinstance(result, dict) else "")
