"""
fleet_agent/engines/voice_resemblyzer.py
Speaker embeddings with Resemblyzer (`pip install resemblyzer`; pulls in PyTorch).
Input is a mono 16-bit PCM WAV — the browser tab records and encodes it.

Status: written against Resemblyzer's documented API but NOT run here (PyTorch
and a microphone were not available). The 0.75 threshold is a commonly quoted
starting value, not calibrated for your microphones or room: record genuine and
impostor samples and pick your own. A recording of the operator's voice will
also pass — this is not liveness detection.
"""
from __future__ import annotations

import io
import wave

from ..biometrics import BiometricError, EngineUnavailable


class ResemblyzerVoiceEngine:
    kind = "voice"
    model_id = "resemblyzer-ge2e"
    threshold = 0.75
    needed_samples = 3
    consistency_floor = 0.55

    def __init__(self) -> None:
        try:
            from resemblyzer import VoiceEncoder, preprocess_wav
        except ImportError:
            raise EngineUnavailable("install resemblyzer to enable voiceprints") from None
        self._preprocess = preprocess_wav
        self._enc = VoiceEncoder("cpu")

    def embed(self, wav_bytes: bytes) -> list[float]:
        import numpy as np
        try:
            with wave.open(io.BytesIO(wav_bytes)) as w:
                rate, ch, width, frames = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.readframes(w.getnframes())
        except (wave.Error, EOFError):
            raise BiometricError("the recording is not a valid WAV file") from None
        if ch != 1 or width != 2:
            raise BiometricError("recording must be mono 16-bit")
        pcm = np.frombuffer(frames, np.int16).astype(np.float32) / 32768.0
        if len(pcm) < rate * 1.5:
            raise BiometricError("recording too short — speak for about 3 seconds")
        speech = self._preprocess(pcm, source_sr=rate)
        if len(speech) < 16000:
            raise BiometricError("not enough speech detected — speak closer to the microphone")
        return [float(x) for x in self._enc.embed_utterance(speech)]
