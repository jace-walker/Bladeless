"""Turns a camera snapshot (JPEG bytes) into a 512-number face embedding.

Uses the same Facenet512 + OpenCV pipeline as face_check.py and the old local
server, so embeddings from all three are interchangeable. Photos are decoded in
memory and never saved.

On Vercel the only writable folder is /tmp, so the model weights (~90 MB) are
downloaded there from the Hugging Face Hub the first time an instance needs them,
then reused for as long as that instance stays warm.
"""

import os
import threading
import urllib.request
from pathlib import Path

ON_VERCEL = bool(os.environ.get("VERCEL"))
if ON_VERCEL:
    os.environ.setdefault("DEEPFACE_HOME", "/tmp")  # DeepFace keeps its weights here

MODEL = "Facenet512"
DETECTOR = "opencv"
WEIGHTS_FILE = "facenet512_weights.onnx"
# DeepFace's author publishes the same weights on the Hugging Face Hub.
HF_WEIGHTS_URL = "https://huggingface.co/serengil/deepface/resolve/main/" + WEIGHTS_FILE
MAX_SIDE = 1024  # reject images larger than this on either side

_lock = threading.Lock()  # one face model run at a time per instance
_loaded = False
status = "Not loaded"
error = None


class LivenessFailed(ValueError):
    """The liveness check thinks the face is a photo or screen."""


def _weights_path():
    home = os.environ.get("DEEPFACE_HOME", os.path.expanduser("~"))
    return Path(home) / ".deepface" / "weights" / WEIGHTS_FILE


def _download_weights_from_hugging_face():
    """Fetch the Facenet512 weights from the Hugging Face Hub if they aren't on disk.
    If this fails, DeepFace falls back to downloading them itself."""
    target = _weights_path()
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".part")
    try:
        with urllib.request.urlopen(HF_WEIGHTS_URL, timeout=60) as response, open(partial, "wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
        partial.replace(target)  # only appears under its real name once complete
    except OSError as e:
        partial.unlink(missing_ok=True)
        print(f"Hugging Face download failed ({e}); DeepFace will download the weights itself.")


def load():
    """Load the face models once per instance. Call while holding _lock."""
    global _loaded, status, error
    if _loaded:
        return
    status = "Loading face model"
    try:
        _download_weights_from_hugging_face()
        from deepface import DeepFace
        DeepFace.build_model(MODEL)
        DeepFace.build_model(DETECTOR, task="face_detector")
    except Exception as e:
        status, error = "Error", str(e)[:240]
        raise
    _loaded, status, error = True, "Ready", None


def embed(jpeg, anti_spoofing=False):
    """Return the embedding (a list of 512 floats) for the biggest face in the
    JPEG, or None if there's no face. Raises ValueError for a bad image and
    LivenessFailed if the liveness check rejects the face."""
    import cv2
    import numpy as np

    frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None or max(frame.shape[:2]) > MAX_SIDE:
        raise ValueError("Invalid or oversized image")

    with _lock:
        load()
        from deepface import DeepFace
        from deepface.modules.exceptions import FaceNotDetected, SpoofDetected

        try:
            faces = DeepFace.represent(
                frame,                  # OpenCV gives BGR, which is what DeepFace expects
                model_name=MODEL,
                detector_backend=DETECTOR,
                max_faces=1,            # if several people are in view, use the biggest face
                anti_spoofing=anti_spoofing,
            )
        except FaceNotDetected:
            return None
        except SpoofDetected:
            raise LivenessFailed("Liveness check failed")

    embedding = [float(v) for v in faces[0]["embedding"]]
    if len(embedding) != 512 or not all(np.isfinite(embedding)) or not any(embedding):
        raise ValueError("Invalid face embedding")
    return embedding
