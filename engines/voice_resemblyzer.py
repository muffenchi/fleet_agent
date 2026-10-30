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
import numpy as np

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
        
        try:
            with wave.open(io.BytesIO(wav_bytes)) as w:
                rate, ch, width, frames = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.readframes(w.getnframes())
        except (wave.Error, EOFError):
            raise BiometricError("the recording is not a valid WAV file") from None
        if ch != 1 or width != 2:
            raise BiometricError("recording must be mono 16-bit")
        pcm = np.frombuffer(frames, np.int16).astype(np.float32) / 32768.0
        if len(pcm) < rate * 1.0:
            raise BiometricError("recording too short — speak for at least 1 second")

        rms = float(np.sqrt(np.mean(pcm ** 2)))
        if rms < 0.005:
            raise BiometricError( f"audio is too quiet (RMS {rms:.4f}) — speak louder or move closer to the microphone" )

        speech = self._preprocess(pcm, source_sr=rate)

        # VAD over-trimmed on a short phrase. Fall back to the loudest 1.5 s
        # window of the raw recording. embed_utterance() averages frames, so
        # surrounding silence dilutes but does not break the embedding.
        if len(speech) < 8000:                # < 0.5 s of trimmed speech
            n = min(len(pcm), int(rate * 1.5))
            if len(pcm) > n:
                step = max(1, n // 8)
                best_start, best_rms = 0, -1.0
                for start in range(0, len(pcm) - n + 1, step):
                    r = float(np.sqrt(np.mean(pcm[start:start+n] ** 2)))
                    if r > best_rms:
                        best_rms, best_start = r, start
                speech = pcm[best_start:best_start+n]
            else:
                speech = pcm
            if len(speech) < 8000:
                raise BiometricError( "not enough speech detected — speak the phrase slowly and slightly longer" )

        return [float(x) for x in self._enc.embed_utterance(speech)]



