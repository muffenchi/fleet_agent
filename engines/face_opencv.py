"""
fleet_agent/engines/face_opencv.py
Face embeddings with OpenCV's built-in YuNet detector + SFace recogniser
(cv2.FaceDetectorYN / cv2.FaceRecognizerSF) — only `opencv-python` is needed,
no dlib or GPU stack.

Model files are NOT bundled. Download them from the OpenCV model zoo
(github.com/opencv/opencv_zoo, folders models/face_detection_yunet and
models/face_recognition_sface) and put them in the project models/ directory
or ~/.fleet_agent/models/, or point FLEET_AGENT_YUNET_ONNX /
FLEET_AGENT_SFACE_ONNX at them.

Status: the cv2 classes used here were confirmed to exist in opencv 4.13, but
this engine has not been run against the real model files or a real camera.
The 0.363 cosine threshold is the value in OpenCV's SFace documentation, not
something calibrated on your camera — measure your own genuine/impostor scores.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
import numpy as np
from ..biometrics import BiometricError, EngineUnavailable

PROJECT_MODEL_DIR = Path(__file__).resolve().parents[1] / "models"
USER_MODEL_DIR = Path.home() / ".fleet_agent" / "models"
MODEL_DIR_OVERRIDE = os.getenv("FLEET_AGENT_MODELS")
MODEL_DIRS = ((Path(MODEL_DIR_OVERRIDE).expanduser(),) if MODEL_DIR_OVERRIDE
              else (PROJECT_MODEL_DIR, USER_MODEL_DIR))


def _model_path(env_var: str, filename: str) -> Path:
    override = os.getenv(env_var)
    if override:
        return Path(override).expanduser()
    return next(
        (directory / filename for directory in MODEL_DIRS if (directory / filename).is_file()),
        MODEL_DIRS[0] / filename,
    )


YUNET = _model_path("FLEET_AGENT_YUNET_ONNX", "face_detection_yunet_2023mar.onnx")
SFACE = _model_path("FLEET_AGENT_SFACE_ONNX", "face_recognition_sface_2021dec.onnx")
MIN_FACE_PX = 80


class OpenCVFaceEngine:
    kind = "face"
    model_id = "opencv-yunet2023mar-sface2021dec"
    threshold = 0.363
    needed_samples = 5
    consistency_floor = 0.30

    def __init__(self) -> None:
        try:
            import cv2
        except ImportError:
            raise EngineUnavailable("install opencv-python to enable face recognition") from None
        for p in (YUNET, SFACE):
            if not p.is_file():
                raise EngineUnavailable(f"model file missing: {p}")
        self._cv2 = cv2
        self._det = cv2.FaceDetectorYN.create(str(YUNET), "", (320, 320), 0.8, 0.3, 5000)
        self._rec = cv2.FaceRecognizerSF.create(str(SFACE), "")
        self._lock = threading.Lock()          # the detector's input size is shared state

    def embed(self, jpeg: bytes) -> list[float]:
        import numpy as np
        cv2 = self._cv2
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise BiometricError("the image could not be decoded")
        h, w = img.shape[:2]
        with self._lock:
            self._det.setInputSize((w, h))
            _, faces = self._det.detect(img)
            n = 0 if faces is None else len(faces)
            if n == 0:
                raise BiometricError("no face found — face the camera in good light")
            if n > 1:
                raise BiometricError("more than one face in frame")
            if faces[0][2] < MIN_FACE_PX:
                raise BiometricError("face too small — move closer to the camera")
            feat = self._rec.feature(self._rec.alignCrop(img, faces[0]))
        return [float(x) for x in feat.flatten()]

    def detect(self, jpeg: bytes) -> dict:
        """Detect faces without computing embeddings — for the preview UI.
        Returns:
            {"ok": bool, "reason": str, "faces": [...], "width": int, "height": int}
            reason: "", "no_face", "multiple_faces", "face_too_small", "decode_failed"
        """

        cv2 = self._cv2
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:            return {"ok": False, "reason": "decode_failed", "faces": [], "width": 0, "height": 0}

        h, w = img.shape[:2]
        with self._lock:
            self._det.setInputSize((w, h))
            _, faces = self._det.detect(img)

        n = 0 if faces is None else len(faces)

        def _box(f):
            return {"x": float(f[0]), "y": float(f[1]), "w": float(f[2]), "h": float(f[3]), "conf": float(f[14]) if len(f) > 14 else 1.0}

        if n == 0:            return {"ok": False, "reason": "no_face", "faces": [], "width": w, "height": h}
        if n > 1:             return {"ok": False, "reason": "multiple_faces", "faces": [_box(f) for f in faces], "width": w, "height": h}

        f = faces[0]
        face_px = float(f[2])
        ok = face_px >= MIN_FACE_PX
        return {
                    "ok": ok,
                    "reason": "" if ok else "face_too_small",
                    "faces": [_box(f)],
                    "width": w, "height": h,
                }
