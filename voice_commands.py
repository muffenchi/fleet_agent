"""Local voice command registry — per-user phrase → action mappings.

Stored in ~/.fleet_agent/voice_commands.json. Deliberately NOT in Captain:
Captain holds identity (voiceprint/face biometric) but not the phrase list,
because phrases are operator-local configuration.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DB_PATH = Path.home() / ".fleet_agent" / "voice_commands.json"


def _load() -> dict:
    try:
        return json.loads(DB_PATH.read_text())
    except FileNotFoundError:
        log.debug("voice command registry does not exist yet path=%s", DB_PATH)
        return {"version": 1, "users": {}}
    except json.JSONDecodeError:
        log.exception("voice command registry contains invalid JSON path=%s", DB_PATH)
        return {"version": 1, "users": {}}
    except OSError:
        log.exception("voice command registry could not be read path=%s", DB_PATH)
        return {"version": 1, "users": {}}


def _save(data: dict) -> None:
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = DB_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(DB_PATH)
    except OSError:
        log.exception("voice command registry could not be saved path=%s", DB_PATH)
        raise


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
    log.info("voice command registry added id=%s action=%s", entry["id"], action)
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
    log.info("voice command registry updated id=%s action=%s", entry_id, action)
    return entry


def remove(username: str, entry_id: str) -> bool:
    data = _load()
    lst = data["users"].get(username, [])
    for i, e in enumerate(lst):
        if e["id"] == entry_id:
            del lst[i]
            _save(data)
            log.info("voice command registry removed id=%s", entry_id)
            return True
    return False


def match(username: str, text: str) -> Optional[dict]:
    """Return the entry whose normalized phrase equals the whole text (exact)."""
    norm = normalize(text)
    if not norm:
        return None
    for entry in _load()["users"].get(username, []):
        if entry["normalized"] == norm:
            log.info("custom voice command matched action=%s id=%s",
                     entry["action"], entry["id"])
            return entry
    log.debug("no custom voice command matched normalized=%r", norm)
    return None

def add_or_update(username: str, phrase: str, action: str, *, wake_word: str = "captain") -> dict:
    """One phrase per action. Re-uses the entry's id when replacing."""
    data = _load()
    phrase = strip_wake_word(phrase, wake_word)      # ← strip before normalising
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
            log.info("voice command registry replaced action=%s id=%s",
                     action, existing["id"])
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
    log.info("voice command registry added id=%s action=%s", entry["id"], action)
    return entry


def strip_wake_word(phrase: str, wake_word: str = "captain") -> str:
    """Normalise a phrase by removing a leading wake word, so a phrase
    saved as 'captain arm the drone' matches the runtime which strips the
    wake word before comparing."""
    norm = normalize(phrase)
    wake = normalize(wake_word)
    if not wake:
        return norm
    for prefix in (f"hey {wake} ", f"{wake} "):
        if norm.startswith(prefix):
            return norm[len(prefix):].strip()
    return norm
