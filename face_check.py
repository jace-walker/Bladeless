"""
Face check-in, milestone 1: enroll people from your webcam, then recognize them live.

Only face *embeddings* (512 numbers per photo) are saved, to faces.json next to this
script. No photos are ever written to disk. In milestone 2, faces.json gets replaced
by a Neon table with a pgvector column.

Setup (once, in a virtual environment):
    pip install deepface
    pip uninstall -y opencv-contrib-python-headless   # this build can't open windows
    pip install opencv-contrib-python

Usage:
    python face_check.py enroll "Jace"     # capture 5 shots of one person
    python face_check.py identify          # live recognition
    python face_check.py list              # who's enrolled
    python face_check.py delete "Jace"     # remove someone's face data

Click the video window before pressing keys, or your key presses go to the terminal.
"""

import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from deepface import DeepFace
from deepface.modules.exceptions import FaceNotDetected

MODEL = "Facenet512"   # produces 512-number embeddings
DETECTOR = "opencv"    # fast face finder; "retinaface" is more accurate but slower
THRESHOLD = 0.30       # max cosine distance to count as a match (DeepFace's tuned value
                       # for Facenet512). Watch the printed distances and adjust.
CAMERA_INDEX = None    # None = find the Mac's built-in camera automatically.
                       # Set a number (0, 1, 2) to force a specific camera.
SHOTS_PER_PERSON = 5
SECONDS_BETWEEN_CHECKS = 0.5   # recognition is slow-ish, so don't run it every frame
DB_FILE = Path(__file__).with_name("faces.json")
WINDOW = "Face check-in (Q to quit)"

GREEN = (80, 200, 80)
RED = (60, 60, 230)
WHITE = (255, 255, 255)


# ---------- storage (milestone 2 swaps this for Neon) ----------

def load_db():
    """Return {name: [embedding, embedding, ...]}."""
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text())
    return {}


def save_db(db):
    DB_FILE.write_text(json.dumps(db))


# ---------- face math ----------

def embed(frame):
    """Return (embedding, face_box) for the biggest face in the frame, or (None, None)."""
    try:
        faces = DeepFace.represent(
            frame,                  # OpenCV frames are BGR, which is what DeepFace expects
            model_name=MODEL,
            detector_backend=DETECTOR,
            max_faces=1,            # if several people are in view, use the biggest face
        )
    except FaceNotDetected:
        return None, None
    face = faces[0]
    return np.array(face["embedding"], dtype=np.float32), face["facial_area"]


def cosine_distance(a, b):
    """0 = identical direction, bigger = less alike. Same math as pgvector's <=> operator."""
    return 1.0 - float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def best_match(embedding, db):
    """Compare against every stored embedding and return (closest_name, distance)."""
    best_name, best_dist = None, float("inf")
    for name, vectors in db.items():
        for vector in vectors:
            dist = cosine_distance(embedding, np.array(vector, dtype=np.float32))
            if dist < best_dist:
                best_name, best_dist = name, dist
    return best_name, best_dist


# ---------- webcam helpers ----------

def find_builtin_camera():
    """Return the OpenCV index of the Mac's built-in camera, or None if we can't tell.

    OpenCV numbers cameras in the same order macOS lists them, so we ask macOS for
    that list and look for the one it marks as built in (not an iPhone or USB camera).
    Needs: pip install pyobjc-framework-AVFoundation
    """
    if sys.platform != "darwin":
        return None
    try:
        import AVFoundation
    except ImportError:
        print("Tip: pip install pyobjc-framework-AVFoundation to auto-pick the built-in camera.")
        return None
    devices = AVFoundation.AVCaptureDevice.devicesWithMediaType_(AVFoundation.AVMediaTypeVideo)
    for index, device in enumerate(devices):
        if device.deviceType() == AVFoundation.AVCaptureDeviceTypeBuiltInWideAngleCamera:
            print(f"Built-in camera found: {device.localizedName()}")
            return index
    return None


def open_camera():
    """Open the chosen camera, giving it a couple of seconds to warm up."""
    if CAMERA_INDEX is not None:
        indexes = [CAMERA_INDEX]
    else:
        builtin = find_builtin_camera()
        indexes = [i for i in [builtin, 0, 1, 2] if i is not None]
        indexes = list(dict.fromkeys(indexes))   # drop duplicates, keep order
    for index in indexes:
        cam = cv2.VideoCapture(index)
        if not cam.isOpened():
            continue
        for _ in range(20):          # about 2 seconds of retries
            ok, _ = cam.read()
            if ok:
                print(f"Using camera {index}.")
                return cam
            time.sleep(0.1)
        cam.release()
    sys.exit(
        "Couldn't read from the webcam. On a Mac, allow camera access for your terminal "
        "or code editor in System Settings > Privacy & Security > Camera, then fully quit "
        "and reopen that app. Also close any other app that's using the camera."
    )


def load_model():
    """Load both models up front so download problems show up now, not mid-video."""
    print("Loading face models (the first run downloads about 90 MB)...")
    DeepFace.build_model(MODEL)
    DeepFace.build_model(DETECTOR, task="face_detector")


def draw_label(img, text, x, y, color):
    """Draw text on a solid background so it's readable on any video."""
    (w, h), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
    cv2.rectangle(img, (x, y - h - 12), (x + w + 10, y), color, -1)
    cv2.putText(img, text, (x + 5, y - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.7, WHITE, 2)


def draw_box(img, box, color):
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    cv2.rectangle(img, (x, y), (x + w, y + h), color, 2)


# ---------- commands ----------

def enroll(name):
    load_model()
    cam = open_camera()
    captured = []
    print(f"Enrolling {name}. Press SPACE to capture; turn your head slightly between shots.")

    while len(captured) < SHOTS_PER_PERSON:
        ok, frame = cam.read()
        if not ok:
            break
        frame = cv2.flip(frame, 1)  # mirror view feels natural
        display = frame.copy()
        draw_label(display, f"{name}: SPACE to capture ({len(captured)}/{SHOTS_PER_PERSON})",
                   10, 40, GREEN)
        cv2.imshow(WINDOW, display)

        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break
        if key == ord(" "):
            embedding, _ = embed(frame)
            if embedding is None:
                print("  No face found. Face the camera with good lighting and try again.")
                continue
            captured.append(embedding.tolist())
            print(f"  Captured {len(captured)}/{SHOTS_PER_PERSON}")

    cam.release()
    cv2.destroyAllWindows()

    if not captured:
        print("Nothing captured, so nothing saved.")
        return
    db = load_db()
    db.setdefault(name, []).extend(captured)
    save_db(db)
    print(f"Saved {len(captured)} embeddings for {name} ({len(db[name])} total).")


def identify():
    db = load_db()
    if not db:
        sys.exit('No one is enrolled yet. Run: python face_check.py enroll "Your Name"')
    load_model()
    cam = open_camera()
    print(f"Watching for {len(db)} enrolled people. Match threshold: {THRESHOLD}")

    last_check = 0.0
    result = None  # (box, name or None, distance) from the most recent check

    while True:
        ok, frame = cam.read()
        if not ok:
            break
        frame = cv2.flip(frame, 1)

        if time.time() - last_check >= SECONDS_BETWEEN_CHECKS:
            last_check = time.time()
            embedding, box = embed(frame)
            if embedding is None:
                result = None
            else:
                name, dist = best_match(embedding, db)
                matched = dist <= THRESHOLD
                result = (box, name if matched else None, dist)
                verdict = "MATCH" if matched else "unknown"
                print(f"closest: {name:<15} distance: {dist:.3f}  -> {verdict}")

        if result:
            box, name, dist = result
            color = GREEN if name else RED
            text = f"{name} ({dist:.2f})" if name else f"Unknown ({dist:.2f})"
            draw_box(frame, box, color)
            draw_label(frame, text, box["x"], box["y"], color)

        cv2.imshow(WINDOW, frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cam.release()
    cv2.destroyAllWindows()


def list_people():
    db = load_db()
    if not db:
        print("No one enrolled.")
    for name, vectors in db.items():
        print(f"{name}: {len(vectors)} embeddings")


def delete(name):
    db = load_db()
    if db.pop(name, None) is None:
        print(f"No one named {name!r} is enrolled.")
        return
    save_db(db)
    print(f"Deleted all face data for {name}.")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["enroll"] and len(args) == 2:
        enroll(args[1])
    elif args == ["identify"]:
        identify()
    elif args == ["list"]:
        list_people()
    elif args[:1] == ["delete"] and len(args) == 2:
        delete(args[1])
    else:
        print(__doc__)
