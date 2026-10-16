"""Turns a camera snapshot (JPEG bytes) into a 512-number face embedding.

Faces are found with YuNet, OpenCV's small neural-network face detector, then
turned into numbers with Facenet512. face_check.py uses the same pair, so
embeddings from both are interchangeable. Photos are decoded in memory and
never saved.

YuNet replaced OpenCV's older Haar cascade detector, which missed faces in dim
light and at angles. Changing the detector changes how faces are cropped, so
people enrolled with the old detector should delete their face data and enroll
again. To make YuNet accept less certain faces (say, in a dark room), set the
environment variable yunet_score_threshold below its default of 0.9 (DeepFace
reads it).

On Vercel the only writable folder is /tmp, so the model weights are downloaded
there the first time an instance needs them: Facenet512 (~90 MB) from the
Hugging Face Hub and YuNet (~230 KB) from OpenCV's GitHub. They are reused for as
long as that instance stays warm.
"""

import os
import threading
import urllib.request
from pathlib import Path

ON_VERCEL = bool(os.environ.get("VERCEL"))
if ON_VERCEL:
    os.environ.setdefault("DEEPFACE_HOME", "/tmp")  # DeepFace keeps its weights here

MODEL = "Facenet512"
DETECTOR = "yunet"  # OpenCV's neural-network face detector
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


def warm():
    """Load the models now, so the first check-in or photo doesn't wait ~10 s for them."""
    with _lock:
        load()


def _faces(jpeg, anti_spoofing, max_faces):
    """Run the detector and Facenet512 on a JPEG. Returns DeepFace's results,
    biggest face first, or [] if there's no face."""
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
                max_faces=max_faces,    # keeps the biggest faces
                anti_spoofing=anti_spoofing,
            )
        except FaceNotDetected:
            return []
        except SpoofDetected:
            raise LivenessFailed("Liveness check failed")
    return sorted(faces, key=lambda f: f["facial_area"]["w"] * f["facial_area"]["h"], reverse=True)


def _clean(values):
    import numpy as np
    embedding = [float(v) for v in values]
    if len(embedding) != 512 or not all(np.isfinite(embedding)) or not any(embedding):
        raise ValueError("Invalid face embedding")
    return embedding


def embed(jpeg, anti_spoofing=False):
    """Return the embedding (a list of 512 floats) for the biggest face in the
    JPEG, or None if there's no face. Used for enrollment photos. Raises
    ValueError for a bad image and LivenessFailed if the liveness check rejects
    the face."""
    faces = _faces(jpeg, anti_spoofing, max_faces=1)
    return _clean(faces[0]["embedding"]) if faces else None


MAX_FACES = 4            # most faces checked in one kiosk frame
NEAR_FACE_RATIO = 0.5    # other faces must be at least half as wide as the biggest one


def embed_faces(jpeg, anti_spoofing=False):
    """Return every face near the camera, biggest first, as dicts with
    'embedding' (512 floats) and 'box' ({x, y, w, h} in the JPEG's pixels).
    People much farther back than the nearest face (narrower than half its
    width) are ignored, so someone walking past in the background isn't
    checked in. Returns [] if there's no face."""
    faces = _faces(jpeg, anti_spoofing, max_faces=MAX_FACES)
    if not faces:
        return []
    widest = faces[0]["facial_area"]["w"]
    near = [faces[0]] + [f for f in faces[1:] if f["facial_area"]["w"] >= NEAR_FACE_RATIO * widest]
    return [
        dict(embedding=_clean(f["embedding"]),
             box={k: int(f["facial_area"][k]) for k in ("x", "y", "w", "h")})
        for f in near
    ]
