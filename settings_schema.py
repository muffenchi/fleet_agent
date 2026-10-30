"""
fleet_agent/settings_schema.py
Defaults and validation for the settings scopes the agent keeps in Captain's
database (PUT /agent/settings/{scope}). Unknown keys are dropped, so a stale or
hostile blob can never push arbitrary fields into the running agent.
"""
from __future__ import annotations

import re
from typing import Any
from .commands import ACTIONS, PSEUDO_ACTIONS

DEFAULTS: dict[str, dict[str, Any]] = {
    "joystick": {
        "deadzone": 0.15,
        "hold_ms": 800,
        # Defaults — operator can rebind any button in the Joystick tab.
        "bindings": {
            "0":  "select_drone",     # open detail panel (same as right-click "Select Drone")
            "1":  "context_menu",     # open right-click menu (drone or map)
            "2":  "deselect_drone",   # clear selection + focus
            "3":  "focus_next",       # cycle to next drone
            "4":  "zoom_in",
            "5":  "zoom_out",
            "10": "voice_lock_toggle",
            "11": "nav_toggle",       # enable/disable joystick navigation
        },
        "hat_bindings": {},
    },
    "voice": {  "stt_engine": "webspeech",
            "language": "en-US",
            "microphone_id": "",
            "wake_word": "captain",
            "require_wake_word": True,
            "unlock_ttl_s": 300,
            "unlock_factors": ["password"],
            "confirm_window_s": 8, },
    "camera": { "device_id": "" },
    "verification": {
        "face_threshold":              0.363,   # SFace default
        "voice_threshold":             0.75,    # Resemblyzer default
        "voice_threshold_with_face":   0.45,    # used when face matched
        "min_voice_audio_s":           0.5,     # VAD floor
        "face_assist_voice_commands":  True,    # face can substitute for voice
        "continuous_face_check":       False,   # future: live camera during listening
    },
}

 

_NAV_BINDING   = "nav_toggle"          # kept for backwards-compat with saved blobs
_HAT_KEY       = re.compile(r"^[0-3]:(-?1|0),(-?1|0)$")
_BINDABLE      = set(ACTIONS) | set(PSEUDO_ACTIONS)   # anything a button may be bound to
UNLOCK_FACTORS = ("password", "voiceprint", "face")

def _joystick(raw: dict) -> dict:
    out: dict[str, Any] = {}
    if "deadzone" in raw:      out["deadzone"] = _num(raw["deadzone"], 0, 0.9, "deadzone")
    if "hold_ms" in raw:       out["hold_ms"]  = _num(raw["hold_ms"], 300, 3000, "hold_ms", True)

    if "bindings" in raw:
        b = raw["bindings"]
        if not isinstance(b, dict) or len(b) > 64:
            raise SettingsError("bindings must be an object with at most 64 buttons")
        clean = {}
        for k, v in b.items():
            if not (isinstance(k, str) and k.isdigit() and int(k) < 64):
                raise SettingsError(f"button {k!r} is not a button number 0-63")
            if v not in _BINDABLE:
                raise SettingsError(f"button {k} is bound to unknown action {v!r}")
            clean[k] = v
        out["bindings"] = clean

    if "hat_bindings" in raw:
        hb = raw["hat_bindings"]
        if not isinstance(hb, dict) or len(hb) > 32:
            raise SettingsError("hat_bindings must be an object with at most 32 entries")
        clean_h: dict[str, str] = {}
        for k, v in hb.items():
            if not (isinstance(k, str) and _HAT_KEY.match(k)):
                raise SettingsError(f"hat key {k!r} must look like '<hat>:<x>,<y>'")
            if v not in ACTIONS:                 # hat binds only real actions, not UI nav
                raise SettingsError(f"hat {k} is bound to unknown action {v!r}")
            clean_h[k] = v
        out["hat_bindings"] = clean_h
    return out





 

class SettingsError(ValueError):
    pass


def _num(v, lo, hi, name, integer=False):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise SettingsError(f"{name} must be a number")
    if not lo <= v <= hi:
        raise SettingsError(f"{name} must be between {lo} and {hi}")
    return int(v) if integer else float(v)


def _str(v, name, maxlen, pattern=None):
    if not isinstance(v, str) or len(v) > maxlen:
        raise SettingsError(f"{name} must be text up to {maxlen} characters")
    if pattern and v and not re.fullmatch(pattern, v):
        raise SettingsError(f"{name} has unsupported characters")
    return v


def _bool(v, name):
    if not isinstance(v, bool):
        raise SettingsError(f"{name} must be true or false")
    return v


_NAV_BINDING = "nav_toggle"
_HAT_KEY = re.compile(r"^[0-3]:(-?1|0),(-?1|0)$")


def _voice(raw: dict) -> dict:
    out: dict[str, Any] = {}
    if "stt_engine" in raw:   out["stt_engine"] = _str(raw["stt_engine"], "stt_engine", 32, r"[a-z0-9_-]+")
    if "language" in raw:     out["language"] = _str(raw["language"], "language", 16, r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*")
    if "microphone_id" in raw: out["microphone_id"] = _str(raw["microphone_id"], "microphone_id", 256)
    if "wake_word" in raw:
        w = _str(raw["wake_word"], "wake_word", 24, r"[A-Za-z0-9 ]+").strip().lower()
        out["wake_word"] = w
    if "require_wake_word" in raw: out["require_wake_word"] = _bool(raw["require_wake_word"], "require_wake_word")
    if "unlock_ttl_s" in raw:      out["unlock_ttl_s"] = _num(raw["unlock_ttl_s"], 30, 3600, "unlock_ttl_s", True)
    if "confirm_window_s" in raw:  out["confirm_window_s"] = _num(raw["confirm_window_s"], 3, 30, "confirm_window_s", True)
    if "unlock_factors" in raw:
        f = raw["unlock_factors"]
        if (not isinstance(f, list) or not f or len(set(f)) != len(f)
                or any(x not in UNLOCK_FACTORS for x in f)):
            raise SettingsError(f"unlock_factors must be a non-empty list from {UNLOCK_FACTORS}")
        out["unlock_factors"] = list(f)
    if out.get("require_wake_word") and not (out.get("wake_word") or DEFAULTS["voice"]["wake_word"]):
        raise SettingsError("a wake word is required while 'require wake word' is on")
    return out


def _camera(raw: dict) -> dict:
    return {"device_id": _str(raw["device_id"], "device_id", 256)} if "device_id" in raw else {}

def _verification(raw: dict) -> dict:
    out: dict[str, Any] = {}
    if "face_threshold" in              raw:        out["face_threshold"]               = _num(raw["face_threshold"]                , 0.2, 0.8  , "face_threshold")
    if "voice_threshold" in             raw:        out["voice_threshold"]              = _num(raw["voice_threshold"]               , 0.4, 0.95 , "voice_threshold")
    if "voice_threshold_with_face" in   raw:        out["voice_threshold_with_face"]    = _num(raw["voice_threshold_with_face"]     , 0.2, 0.9  , "voice_threshold_with_face")
    if "min_voice_audio_s" in           raw:        out["min_voice_audio_s"]            = _num(raw["min_voice_audio_s"]             , 0.3, 3.0  , "min_voice_audio_s")
    if "face_assist_voice_commands" in  raw:        out["face_assist_voice_commands"]   = _bool(raw["face_assist_voice_commands"]               , "face_assist_voice_commands")
    if "continuous_face_check" in       raw:        out["continuous_face_check"]        = _bool(raw["continuous_face_check"]                    , "continuous_face_check")
    return out

_CLEANERS = {"joystick": _joystick, "voice": _voice, "camera": _camera, "verification": _verification}
SCOPES = tuple(_CLEANERS)


def clean(scope: str, raw: Any) -> dict:
    if scope not in _CLEANERS:
        raise SettingsError(f"unknown settings scope {scope!r}")
    if not isinstance(raw, dict):
        raise SettingsError("settings must be an object")
    return _CLEANERS[scope](raw)


def merged(scope: str, stored: Any) -> dict:
    """Defaults overlaid with whatever Captain returned; a bad stored blob falls
    back to defaults instead of breaking login."""
    base = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
            for k, v in DEFAULTS[scope].items()}
    try:
        base.update(clean(scope, stored or {}))
    except SettingsError:
        pass
    return base



