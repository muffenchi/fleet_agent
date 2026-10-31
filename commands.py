"""
fleet_agent/commands.py
Command builders. The wire format mirrors menus.js mavCmd() and is accepted by
captain/api/command_parser.py's parse_and_dispatch() through POST /command:

    {"type": "mavlink", "droneId": str, "sys_id": int, "class": "COMMAND_LONG",
     "command": int, "params": {param1..param7}, "label": str}

plus two fields the agent adds:
    "source": "agent"        — /command's own marker for "came from a local agent"
    "source_hint": "voice" | "joystick" | "agent"
                             — which header icon(s) Captain flashes (needs the
                               small server.py patch shipped with this project)

`confirm=True` marks actions that start or stop motors. The joystick must hold
the button for hold_ms and voice must be confirmed before they are sent.
Every command id here is in command_parser.ALLOWED_MAVLINK_COMMANDS.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

# Pseudo-actions: UI navigation, never sent as MAVLink commands.
# Bindable to buttons the same way ACTIONS entries are; JoystickMapper routes
# them through CaptainLink.send_nav() instead of execute().
PSEUDO_ACTIONS: frozenset[str] = frozenset({
    "nav_toggle",
    "voice_lock_toggle", 
    "select_drone",
    "deselect_drone",
    "focus_next",
    "focus_prev",
    "context_menu",
    "zoom_in",
    "zoom_out",
    "panel_toggle",
    "cancel",
})


@dataclass(frozen=True)
class Action:
    name: str
    command: int
    params: dict
    label: str          # same wording as menus.js, so the operator log reads the same
    confirm: bool


ACTIONS: dict[str, Action] = {a.name: a for a in (
    Action("arm",     400, {"param1": 1}, "Arm", True),
    Action("disarm",  400, {"param1": 0}, "Disarm", True),
    Action("takeoff", 22,  {"param7": 25}, "Takeoff", True),
    Action("land",    21,  {}, "Land", False),
    Action("rtl",     20,  {}, "RTL", False),
    Action("hold",    17,  {}, "Loiter", False),
    Action("pause",   193, {"param1": 0}, "Pause mission", False),
    Action("resume",  193, {"param1": 1}, "Resume mission", False),
)}

SOURCES = ("agent", "voice", "joystick")


def drone_sys_id(drone_id: str) -> int:
    """menus.js droneSysId(): trailing digits of the ID, else 1."""
    m = re.search(r"(\d+)\s*$", str(drone_id))
    return int(m.group(1)) if m else 1


def build_action(action: str, drone_id: Optional[str], *, takeoff_alt_m: float = 25,
                 source_hint: str = "agent") -> dict:
    if action not in ACTIONS:
        raise KeyError(f"unknown action {action!r}")
    if source_hint not in SOURCES:
        raise ValueError(f"source_hint must be one of {SOURCES}")
    spec = ACTIONS[action]
    params = dict(spec.params)
    if spec.name == "takeoff":
        params["param7"] = float(takeoff_alt_m)
    target = str(drone_id or "").strip()
    return {
        "type": "mavlink",
        "droneId": target,                       # "" → Captain decides
        "sys_id":  drone_sys_id(target) if target else 0,
        "class": "COMMAND_LONG",
        "command": spec.command,
        "params": {**{f"param{i}": 0 for i in range(1, 8)}, **params},
        "label": f"{spec.label} ({target or 'auto'})",
        "source": "agent",
        "source_hint": source_hint,
        "ts": int(time.time() * 1000),
    }
