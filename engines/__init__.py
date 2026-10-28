"""Engine loaders. A missing library or model is reported, never fatal: the
agent still runs, and the UI says exactly what to install."""
from __future__ import annotations

import logging

from ..biometrics import EngineUnavailable

log = logging.getLogger(__name__)


def _load(factory) -> dict:
    try:
        return {"engine": factory(), "reason": ""}
    except EngineUnavailable as exc:
        return {"engine": None, "reason": str(exc)}
    except Exception as exc:                       # broken install, bad model file ...
        log.warning("engine failed to load: %s", exc)
        return {"engine": None, "reason": f"{type(exc).__name__}: {exc}"}


def load_engines() -> dict[str, dict]:
    from .face_opencv import OpenCVFaceEngine
    from .voice_resemblyzer import ResemblyzerVoiceEngine
    return {"face": _load(OpenCVFaceEngine), "voice": _load(ResemblyzerVoiceEngine)}
