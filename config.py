"""fleet_agent/config.py — command-line / environment configuration."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 8780


def _from_agent_json() -> str:
    """agent.json is what Captain's GET /agent/agent.json produces:
    {"server": "http://<drone>:8766", "version": 1}. Looked for in the working
    directory and ~/.fleet_agent/."""
    for p in (Path("agent.json"), Path.home() / ".fleet_agent" / "agent.json"):
        try:
            return str(json.loads(p.read_text()).get("server", ""))
        except (OSError, ValueError):
            continue
    return ""


@dataclass
class Config:
    port: int = DEFAULT_PORT
    captain_url: str = ""
    open_browser: bool = True
    state_interval_s: float = 5.0      # Captain drops a silent agent after 15 s (routes/agent.py)

    @classmethod
    def build(cls, port: int | None, captain: str | None, open_browser: bool = True) -> "Config":
        return cls(port=port or int(os.getenv("FLEET_AGENT_PORT", DEFAULT_PORT)),
                   captain_url=captain or os.getenv("CAPITAN_URL", "") or _from_agent_json(),
                   open_browser=open_browser)
