"""
fleet_agent/biometrics.py
Template math and enrolment bookkeeping for face and voice. Engines (the part
that turns a JPEG or WAV into an embedding vector) are pluggable and live in
fleet_agent/engines/; nothing here depends on OpenCV or any ML library.

What is stored: unit-length embedding vectors only. Photos and audio are
processed in memory and dropped.

What this is NOT: liveness detection. A photo of the operator or a recording
of their voice can match. Treat a biometric factor as a convenience on top of
the Captain login, never as the only barrier in front of flight commands.
"""
from __future__ import annotations

import math
import secrets
import time
from dataclasses import dataclass, field
from typing import Protocol


class BiometricError(Exception):
    """A sample could not be used (no face, two faces, too quiet, ...)."""


class EngineUnavailable(BiometricError):
    """The engine's library or model files are not installed."""


class Embedder(Protocol):
    kind: str           # "face" | "voice"
    model_id: str       # templates from different models are never compared
    threshold: float    # cosine similarity at or above which two samples match
    needed_samples: int         # samples collected per enrolment
    consistency_floor: float    # enrolment samples must be at least this alike
    def embed(self, data: bytes) -> list[float]: ...


def l2_normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    if n == 0 or not math.isfinite(n):
        raise BiometricError("empty embedding")
    return [x / n for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise BiometricError("embedding sizes differ — different model?")
    return sum(x * y for x, y in zip(a, b))          # inputs are unit length


def best_score(probe: list[float], template: list[list[float]]) -> float:
    p = l2_normalize(probe)
    return max(cosine(p, t) for t in template)


def verify(embedder, data, stored, threshold=None):
    if not stored or not stored.get("template"):
        return False, 0.0, f"no {embedder.kind} template is enrolled for this user"
    if stored.get("model") != embedder.model_id:
        return False, 0.0, (f"enrolled with {stored.get('model')!r} but this agent runs "
                            f"{embedder.model_id!r} — re-enrol")
    try:
        score = best_score(embedder.embed(data), stored["template"])
    except BiometricError as exc:
        return False, 0.0, str(exc)
    limit = threshold if threshold is not None else embedder.threshold
    ok = score >= limit
    return ok, score, "match" if ok else f"does not match (needs ≥ {limit:.3f})"


@dataclass
class Enrollment:
    """Collects samples for one (user, kind) until there are enough to commit."""
    username: str
    kind: str
    admin: str
    needed: int
    consistency_floor: float            # a new sample must resemble the first one
    session_id: str = field(default_factory=lambda: secrets.token_urlsafe(12))
    created: float = field(default_factory=time.monotonic)
    vectors: list[list[float]] = field(default_factory=list)

    def add(self, vec: list[float]) -> None:
        v = l2_normalize(vec)
        if self.vectors and cosine(self.vectors[0], v) < self.consistency_floor:
            raise BiometricError("this sample does not look like the earlier ones — "
                                 "is someone else in front of the camera/microphone?")
        self.vectors.append(v)

    @property
    def ready(self) -> bool:
        return len(self.vectors) >= self.needed

    def template(self) -> list[list[float]]:
        if not self.ready:
            raise BiometricError(f"need {self.needed} samples, have {len(self.vectors)}")
        return [list(v) for v in self.vectors]


class EnrollmentStore:
    TTL_S = 600

    def __init__(self) -> None:
        self._by_id: dict[str, Enrollment] = {}

    def start(self, **kw) -> Enrollment:
        self._gc()
        e = Enrollment(**kw)
        self._by_id[e.session_id] = e
        return e

    def get(self, session_id: str, admin: str) -> Enrollment:
        self._gc()
        e = self._by_id.get(session_id)
        if e is None or e.admin != admin:
            raise BiometricError("enrolment session expired — start again")
        return e

    def drop(self, session_id: str) -> None:
        self._by_id.pop(session_id, None)

    def _gc(self) -> None:
        now = time.monotonic()
        for sid in [s for s, e in self._by_id.items() if now - e.created > self.TTL_S]:
            del self._by_id[sid]
