"""
fleet_agent/voice_trace.py
End-to-end trace of the voice pipeline, one trace id per utterance.

    heard   -> the browser speech engine delivered a FINAL transcript
    send    -> the agent is about to POST /command to Captain (or refused to)
    captain -> Captain's HTTP answer (status, latency, and whether Captain
               echoed our trace id back — proof it ran the patched /command)
    verdict -> VoiceController's final outcome (executed / locked / rejected /
               needs_confirm / ignored / ...). Recorded last, because the
               command is sent from inside the controller before it returns.

The same id is sent to Captain as `trace_id`, so one grep follows a spoken
sentence from the laptop console to Captain's log.

Also keeps the health of the speech engine itself (started / stopped / error
reported by the browser, finals received) because that is the part nobody can
see today: a browser engine that never starts, or dies on "network", looks
identical to "nothing was said".

The ring buffer lives in laptop memory; every event is *also* appended as one
JSON line to a trace file so a post-mortem survives a restart. Overheard
sentences that are not commands stay in the ring buffer and the trace file —
they are never sent to Captain.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import secrets
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

#: Default trace-file location. FLEET_AGENT_TRACE_FILE overrides it; an empty
#: value disables the file (memory-only). Reliability fix: tracing used to be
#: opt-in via an env var nobody set, so after a crash/restart there was nothing
#: left on disk to inspect.
DEFAULT_TRACE_FILE = str(Path.home() / ".fleet_agent" / "voice_trace.jsonl")


def resolve_trace_file(env_value: Optional[str] = None) -> Optional[str]:
    """Trace-file path from the environment, defaulting to ~/.fleet_agent/.
    Returns None only when FLEET_AGENT_TRACE_FILE is explicitly set to ""."""
    if env_value is None:
        env_value = os.environ.get("FLEET_AGENT_TRACE_FILE")
    if env_value is not None:
        return env_value.strip() or None
    return DEFAULT_TRACE_FILE

#: Set by the HTTP handler around VoiceController.handle(); read by Agent.execute()
#: so the command that goes to Captain carries the id of the sentence that caused it.
TRACE: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("fleet_agent_trace", default=None)

STT_KINDS = ("started", "stopped", "error", "fallback")
_STATUSES = ("ignored", "locked", "needs_confirm", "executed", "rejected", "cancelled", "expired")


def new_trace(prefix: str = "vc") -> str:
    return f"{prefix}-{secrets.token_hex(3)}"


class TraceLog:
    def __init__(self, keep: int = 100, coalesce_s: float = 5.0, clock=time.monotonic, wall=time.time,
                 file_path: Optional[str] = None, max_bytes: int = 5_000_000) -> None:
        self.keep, self.coalesce_s, self._clock, self._wall = keep, coalesce_s, clock, wall
        self.max_bytes = max_bytes
        self._items: "OrderedDict[str, dict]" = OrderedDict()
        # Persistent trace file (the in-memory ring buffer alone is not enough
        # when the browser never delivers a final transcript — after a restart
        # nothing is left to inspect). Defaults to ~/.fleet_agent/voice_trace.jsonl;
        # pass file_path=None + env FLEET_AGENT_TRACE_FILE="" only to disable it.
        self._file = Path(file_path).expanduser() if file_path else None
        if self._file:
            try:
                self._file.parent.mkdir(parents=True, exist_ok=True)
                log.info("voice-trace file enabled path=%s", self._file)
            except OSError:
                log.exception("voice-trace file could not be prepared path=%s", self._file)
                self._file = None
        self.counters: dict[str, int] = {k: 0 for k in
                                         ("final", *_STATUSES, "other", "captain_ok", "captain_fail", "unlinked")}
        self.stt: dict[str, Any] = {"engine": "", "state": "unknown", "errors": 0, "finals": 0,
                                    "last_error": "", "last_event_ts": None, "last_final_ts": None}
        self._last_err: tuple[str, str, float] = ("", "", 0.0)

    def _write(self, record: dict) -> None:
        """Append one JSON line to the trace file (best effort — tracing must
        never break the voice pipeline). Rotates at max_bytes so a long-lived
        agent cannot fill the laptop's disk."""
        if not self._file:
            return
        try:
            if self.max_bytes and self._file.exists() and self._file.stat().st_size > self.max_bytes:
                self._file.replace(self._file.with_name(self._file.name + ".1"))
            with self._file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            log.exception("voice-trace file write failed path=%s", self._file)

    # ── per-utterance trace ────────────────────────────────────────────
    def begin(self, tid: str, text: str, engine: str = "", count: bool = True) -> dict:
        """Open a trace (or, for a spoken 'confirm', continue the pending one)."""
        now = self._clock()
        item = self._items.get(tid)
        if item is None:
            item = {"id": tid, "ts": self._wall(), "t0": now, "text": text, "engine": engine,
                    "status": "", "action": "", "drone": "", "detail": "",
                    "captain_http": None, "captain_ms": None, "linked": None, "stages": []}
            self._items[tid] = item
            while len(self._items) > self.keep:
                self._items.popitem(last=False)
        if count:                              # a button press is not a speech-engine final
            self.counters["final"] += 1
            self.stt["finals"] += 1
            self.stt["last_final_ts"] = self._wall()
            if engine:
                self.stt["engine"] = engine
        return self.stage(tid, "heard", text=text, engine=engine)

    def stage(self, tid: str, name: str, **fields: Any) -> dict:
        """Record one stage; returns the event dict (also what gets broadcast)."""
        item = self._items.get(tid)
        at_ms = int((self._clock() - item["t0"]) * 1000) if item else 0
        ev = {"at_ms": at_ms, **fields}
        if item is None:                       # execute() from a joystick button: no 'heard' stage
            item = {"id": tid, "ts": self._wall(), "t0": self._clock(), "text": "", "engine": "",
                    "status": "", "action": "", "drone": "", "detail": "",
                    "captain_http": None, "captain_ms": None, "linked": None, "stages": []}
            self._items[tid] = item
            while len(self._items) > self.keep:
                self._items.popitem(last=False)
        item["stages"].append({"stage": name, **ev})
        self._write({"ts": self._wall(), "id": tid, "stage": name, **ev})
        if name == "verdict":
            st = fields.get("status", "")
            self.counters[st if st in _STATUSES else "other"] += 1
            item["status"] = st
            item["action"] = fields.get("action") or item["action"]
            item["drone"] = fields.get("drone") or item["drone"]
            item["detail"] = fields.get("detail") or ""
            log.info("voice-trace %s verdict status=%s action=%s drone=%s detail=%r",
                     tid, st, item["action"] or "-", item["drone"] or "-", item["detail"])
        elif name == "captain":
            ok = bool(fields.get("ok"))
            self.counters["captain_ok" if ok else "captain_fail"] += 1
            item["captain_http"] = fields.get("http")
            item["captain_ms"] = fields.get("ms")
            item["linked"] = fields.get("linked")
            if ok and fields.get("linked") is False:
                self.counters["unlinked"] += 1
            if not ok:
                item["detail"] = fields.get("detail") or item["detail"]
            log.info("voice-trace %s captain ok=%s http=%s ms=%s linked=%s",
                     tid, ok, item["captain_http"], item["captain_ms"], item["linked"])
        else:
            log.info("voice-trace %s %s %s", tid, name,
                     " ".join(f"{key}={value!r}" for key, value in fields.items()
                              if value not in (None, "")))
        return ev

    # ── speech-engine health, reported by the browser ──────────────────
    def stt_event(self, kind: str, detail: str = "", engine: str = "") -> Optional[dict]:
        """Returns the event to broadcast, or None if it was a repeat inside the
        coalescing window (Chrome restarts a failing engine in a tight loop).
        Reliability fix: 'stopped' is coalesced as well — Chrome's cloud engine
        ends every silence-bounded session with onend, which used to bury real
        errors under an endless stream of stopped events."""
        if kind not in STT_KINDS:
            raise ValueError(f"kind must be one of {STT_KINDS}")
        now = self._clock()
        if kind in ("error", "stopped"):
            last_kind, last_detail, last_at = self._last_err
            if kind == last_kind and detail == last_detail and now - last_at < self.coalesce_s:
                if kind == "error":
                    self.stt["errors"] += 1
                log.debug("coalesced repeated speech-engine %s engine=%r detail=%r",
                          kind, engine or self.stt["engine"], detail)
                return None
            self._last_err = (kind, detail, now)
        if kind == "error":
            self.stt["errors"] += 1
            self.stt["last_error"] = detail[:200]
        self.stt["state"] = {"started": "listening", "stopped": "stopped",
                             "error": "error", "fallback": "fallback"}[kind]
        self.stt["last_event_ts"] = self._wall()
        if engine:
            self.stt["engine"] = engine[:32]
        ev = {"kind": kind, "detail": detail[:200], "engine": self.stt["engine"]}
        self._write({"ts": self._wall(), "stage": "stt", **ev})
        level = logging.ERROR if kind == "error" else (
            logging.WARNING if kind == "fallback" else logging.INFO)
        log.log(level, "speech engine %s engine=%r detail=%r", kind, self.stt["engine"], detail[:200])
        return ev

    # ── read side ──────────────────────────────────────────────────────
    def snapshot(self, limit: int = 30) -> dict:
        now = self._wall()
        stt = dict(self.stt)
        lf = stt.pop("last_final_ts")
        stt["last_final_age_s"] = None if lf is None else round(now - lf, 1)
        recent = []
        for item in reversed(list(self._items.values())[-limit:]):
            row = {k: v for k, v in item.items() if k != "t0"}
            recent.append(row)
        return {"stt": stt, "counters": dict(self.counters), "recent": recent}