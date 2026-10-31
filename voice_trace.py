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

Everything here lives in laptop memory only. Overheard sentences that are not
commands are kept in this ring buffer (and the console log) but are never sent
to Captain.
"""
from __future__ import annotations

import contextvars
import logging
import secrets
import time
from collections import OrderedDict
from typing import Any, Optional

log = logging.getLogger(__name__)

#: Set by the HTTP handler around VoiceController.handle(); read by Agent.execute()
#: so the command that goes to Captain carries the id of the sentence that caused it.
TRACE: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("fleet_agent_trace", default=None)

STT_KINDS = ("started", "stopped", "error")
_STATUSES = ("ignored", "locked", "needs_confirm", "executed", "rejected", "cancelled", "expired")


def new_trace(prefix: str = "vc") -> str:
    return f"{prefix}-{secrets.token_hex(3)}"


class TraceLog:
    def __init__(self, keep: int = 100, coalesce_s: float = 5.0, clock=time.monotonic, wall=time.time) -> None:
        self.keep, self.coalesce_s, self._clock, self._wall = keep, coalesce_s, clock, wall
        self._items: "OrderedDict[str, dict]" = OrderedDict()
        self.counters: dict[str, int] = {k: 0 for k in
                                         ("final", *_STATUSES, "other", "captain_ok", "captain_fail", "unlinked")}
        self.stt: dict[str, Any] = {"engine": "", "state": "unknown", "errors": 0, "finals": 0,
                                    "last_error": "", "last_event_ts": None, "last_final_ts": None}
        self._last_err: tuple[str, str, float] = ("", "", 0.0)

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
        coalescing window (Chrome restarts a failing engine in a tight loop)."""
        if kind not in STT_KINDS:
            raise ValueError(f"kind must be one of {STT_KINDS}")
        now = self._clock()
        if kind == "error":
            last_kind, last_detail, last_at = self._last_err
            if detail == last_detail and now - last_at < self.coalesce_s:
                self.stt["errors"] += 1
                log.debug("coalesced repeated speech-engine error engine=%r detail=%r",
                          engine or self.stt["engine"], detail)
                return None
            self._last_err = ("error", detail, now)
            self.stt["errors"] += 1
            self.stt["last_error"] = detail[:200]
        self.stt["state"] = {"started": "listening", "stopped": "stopped", "error": "error"}[kind]
        self.stt["last_event_ts"] = self._wall()
        if engine:
            self.stt["engine"] = engine[:32]
        log.log(logging.ERROR if kind == "error" else logging.INFO,
                "speech engine %s engine=%r detail=%r", kind, self.stt["engine"], detail[:200])
        return {"kind": kind, "detail": detail[:200], "engine": self.stt["engine"]}

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