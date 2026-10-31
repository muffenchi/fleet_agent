"""
fleet_agent/stt_local.py
Server-side, fully offline speech-to-text for the agent laptop.

The browser's Web Speech engine streams audio to a vendor cloud (Chrome/Edge),
so on an offline laptop it dies with "network after a couple of tries". This
module is the local backup: the browser's `stt/local.js` engine records an
utterance and POSTs it here as 16 kHz mono WAV; we transcribe it on-device with
whichever backend is installed — no network at any point.

Backends, tried in this order (both optional):
  1. Vosk            pip install vosk         + a small model dir
  2. faster-whisper  pip install faster-whisper  (auto-downloads its model
     once, then works fully offline)

Model locations, env-overridable so field laptops can carry them on a stick:
  FLEET_AGENT_VOSK_MODEL      default ~/.fleet_agent/models/vosk
  FLEET_AGENT_WHISPER_MODEL   default ~/.fleet_agent/models/whisper  (HF cache
                              dir; the model name itself comes from
                              FLEET_AGENT_WHISPER_NAME, default "base.en")

Everything runs in a worker thread (the FastAPI route uses run_in_threadpool);
transcription is CPU-bound and must never block the event loop.
"""
from __future__ import annotations

import io
import json
import logging
import os
import wave
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

VOSK_MODEL_DIR = Path(os.environ.get(
    "FLEET_AGENT_VOSK_MODEL", str(Path.home() / ".fleet_agent" / "models" / "vosk"))).expanduser()
WHISPER_CACHE_DIR = Path(os.environ.get(
    "FLEET_AGENT_WHISPER_MODEL", str(Path.home() / ".fleet_agent" / "models" / "whisper"))).expanduser()
WHISPER_MODEL_NAME = os.environ.get("FLEET_AGENT_WHISPER_NAME", "base.en")

NOT_INSTALLED = (
    "No local STT engine installed on this laptop. Install one of: "
    "'pip install vosk' (plus a model in ~/.fleet_agent/models/vosk) or "
    "'pip install faster-whisper'. The browser cloud engine needs internet; "
    "this local fallback does not."
)


def _wav_rate(data: bytes) -> int:
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            return wf.getframerate()
    except wave.Error:
        return 16000


class LocalStt:
    """Lazy-loading wrapper around whichever local backend exists."""

    def __init__(self) -> None:
        self._vosk_rec = None
        self._whisper = None
        self.backend: Optional[str] = None
        self.reason: str = ""          # why unavailable, when backend is None
        self._loaded = False

    # ── probing / loading ───────────────────────────────────────────────
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._vosk_rec = self._try_vosk()
        if self._vosk_rec is not None:
            self.backend = "vosk"
            log.info("local STT backend: vosk model=%s", VOSK_MODEL_DIR)
            return
        self._whisper = self._try_whisper()
        if self._whisper is not None:
            self.backend = "faster-whisper"
            log.info("local STT backend: faster-whisper name=%s", WHISPER_MODEL_NAME)
            return
        self.reason = NOT_INSTALLED
        log.warning("local STT unavailable: %s", NOT_INSTALLED)

    def _try_vosk(self):
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel
        except ImportError:
            return None
        if not VOSK_MODEL_DIR.is_dir():
            log.warning("vosk is installed but no model found at %s", VOSK_MODEL_DIR)
            return None
        try:
            SetLogLevel(-1)
            model = Model(str(VOSK_MODEL_DIR))
            rate = model.GetSampleRate() if hasattr(model, "GetSampleRate") else 16000
            rec = KaldiRecognizer(model, rate)
            rec.SetWords(False)
            rec._model = model          # keep a reference so it is not GC'd
            log.info("vosk recognizer ready model=%s rate=%s", VOSK_MODEL_DIR, rate)
            return rec
        except Exception:
            log.exception("vosk model at %s failed to load", VOSK_MODEL_DIR)
            return None

    def _try_whisper(self):
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return None
        try:
            WHISPER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            model = WhisperModel(WHISPER_MODEL_NAME, device="cpu", compute_type="int8",
                                 download_root=str(WHISPER_CACHE_DIR))
            log.info("faster-whisper model '%s' loaded (cache=%s)", WHISPER_MODEL_NAME, WHISPER_CACHE_DIR)
            return model
        except Exception as exc:
            log.exception("faster-whisper model '%s' failed to load", WHISPER_MODEL_NAME)
            return None

    @property
    def available(self) -> bool:
        self._load()
        return self.backend is not None

    # ── transcription ───────────────────────────────────────────────────
    def transcribe(self, wav_bytes: bytes, language: str = "en-US") -> str:
        """Return the transcript ('' when nothing was recognised). Raises
        RuntimeError with an actionable message when no backend is installed."""
        self._load()
        if self.backend == "vosk":
            return self._vosk(wav_bytes)
        if self.backend == "faster-whisper":
            return self._whisper_transcribe(wav_bytes, language)
        raise RuntimeError(NOT_INSTALLED)

    def _vosk(self, wav_bytes: bytes) -> str:
        rec = self._vosk_rec
        rec.AcceptWaveform(wav_bytes)
        out = json.loads(rec.FinalResult())
        text = (out.get("text") or "").strip()
        log.debug("vosk transcript=%r", text)
        return text

    def _whisper_transcribe(self, wav_bytes: bytes, language: str) -> str:
        import numpy as np
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            rate, ch, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())
        pcm = np.frombuffer(raw, dtype=np.int16 if width == 2 else np.uint8).astype(np.float32) / 32768.0
        if ch == 2:
            pcm = pcm.reshape(-1, 2).mean(axis=1)
        segs, _info = self._whisper.transcribe(pcm, sr=rate, language=(language or "en")[:2],
                                               beam_size=1, vad_filter=True, condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segs).strip()
        log.debug("faster-whisper transcript=%r", text)
        return text
