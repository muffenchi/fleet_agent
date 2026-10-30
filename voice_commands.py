"""Local voice command registry — per-user phrase → action mappings.

Stored in ~/.fleet_agent/voice_commands.json. Deliberately NOT in Captain:
Captain holds identity (voiceprint/face biometric) but not the phrase list,
because phrases are operator-local configuration.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / ".fleet_agent" / "voice_commands.json"


def _load() -> dict:
    try:
        return json.loads(DB_PATH.read_text())
    except (OSError, ValueError):
        return {"version": 1, "users": {}}


def _save(data: dict) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DB_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(DB_PATH)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())).strip()


def list_user(username: str) -> list[dict]:
    return list(_load()["users"].get(username, []))


def add(username: str, phrase: str, action: str) -> dict:
    data = _load()
    norm = normalize(phrase)
    if not norm:
        raise ValueError("phrase must contain at least one word")
    if len(norm) > 60:
        raise ValueError("phrase must be 60 characters or fewer")
    lst = data["users"].setdefault(username, [])
    for existing in lst:
        if existing["normalized"] == norm:
            raise ValueError(
                f"phrase already registered for {existing['action']!r}"
            )
    entry = {
        "id": f"vc_{int(time.time() * 1000)}",
        "phrase": phrase.strip(),
        "normalized": norm,
        "action": action,
        "created_ts": int(time.time()),
    }
    lst.append(entry)
    _save(data)
    return entry

def update(username: str, entry_id: str, phrase: str, action: str) -> dict:
    """Change phrase and/or action of an existing entry."""
    data = _load()
    norm = normalize(phrase)
    if not norm:
        raise ValueError("phrase must contain at least one word")
    if len(norm) > 60:
        raise ValueError("phrase must be 60 characters or fewer")
    lst = data["users"].get(username, [])
    entry = None
    for e in lst:
        if e["id"] == entry_id:
            entry = e
            break
    if entry is None:
        raise ValueError(f"voice command {entry_id} not found")
    for existing in lst:
        if existing["id"] != entry_id and existing["normalized"] == norm:
            raise ValueError(f"phrase already registered for {existing['action']!r}")
    entry["phrase"]     = phrase.strip()
    entry["normalized"] = norm
    entry["action"]     = action
    _save(data)
    return entry


def remove(username: str, entry_id: str) -> bool:
    data = _load()
    lst = data["users"].get(username, [])
    for i, e in enumerate(lst):
        if e["id"] == entry_id:
            del lst[i]
            _save(data)
            return True
    return False


def match(username: str, text: str) -> Optional[dict]:
    """Return the entry whose normalized phrase equals the whole text (exact)."""
    norm = normalize(text)
    if not norm:
        return None
    for entry in _load()["users"].get(username, []):
        if entry["normalized"] == norm:
            return entry
    return None

def add_or_update(username: str, phrase: str, action: str) -> dict:
    """One phrase per action. Re-uses the entry's id when replacing."""
    data = _load()
    norm = normalize(phrase)
    if not norm:
        raise ValueError("phrase must contain at least one word")
    lst = data["users"].setdefault(username, [])

    for existing in lst:
        if existing["normalized"] == norm and existing["action"] != action:
            raise ValueError(f"phrase already registered for {existing['action']!r}")

    for existing in lst:
        if existing["action"] == action:
            existing["phrase"]     = phrase.strip()
            existing["normalized"] = norm
            _save(data)
            return existing

    entry = {
        "id": f"vc_{int(time.time() * 1000)}",
        "phrase": phrase.strip(),
        "normalized": norm,
        "action": action,
        "created_ts": int(time.time()),
    }
    lst.append(entry)
    _save(data)
    return entry
