"""
Face check-in, milestone 2: members, faces and check-ins live in Neon (Postgres + pgvector).

Only face *embeddings* (512 numbers per shot) are stored. No photos are saved anywhere.
Face matching happens inside the database with pgvector's <=> (cosine distance) operator.

Setup (once):
    pip install "psycopg[binary]" pgvector python-dotenv
    Create a .env file next to this script containing:
        DATABASE_URL=postgresql://...your Neon connection string...
        CAMERA_INDEX=0          (use the number camera_test.py showed for your MacBook)
    python face_check.py setup  # creates or upgrades the tables in Neon (safe to run again)
    python face_check.py seed   # optional: adds the demo events and members from seed.sql

Usage:
    python face_check.py enroll "Jace"              # capture 5 shots of one person
    python face_check.py event "Week 3 Meeting"     # start a new event
    python face_check.py identify                   # live check-in for the latest event
    python face_check.py attendance                 # who checked in to the latest event
    python face_check.py list                       # everyone enrolled
    python face_check.py delete "Jace"              # remove a member and all their data

Click the video window before pressing keys, or your key presses go to the terminal.
"""

import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import psycopg
from deepface import DeepFace
from deepface.modules.exceptions import FaceNotDetected
from dotenv import load_dotenv
from pgvector.psycopg import register_vector

load_dotenv(Path(__file__).with_name(".env"))

MODEL = "Facenet512"   # produces 512-number embeddings
DETECTOR = "yunet"     # OpenCV's neural-network face finder; must match face_engine.py
THRESHOLD = 0.30       # max cosine distance to count as a match. Tune from the printed distances.
CAMERA_INDEX = int(os.getenv("CAMERA_INDEX", "0"))
SHOTS_PER_PERSON = 5
SECONDS_BETWEEN_CHECKS = 0.5   # recognition is slow-ish, so don't run it every frame
MATCHES_TO_CHECK_IN = 2        # same person must match this many checks in a row (~1 second)
SCHEMA_FILE = Path(__file__).with_name("schema.sql")
SEED_FILE = Path(__file__).with_name("seed.sql")
WINDOW = "Face check-in (Q to quit)"

GREEN = (80, 200, 80)
YELLOW = (40, 190, 230)
RED = (60, 60, 230)
WHITE = (255, 255, 255)


# ---------- database ----------

_conn = None  # one shared connection, opened on first use


def database_url():
    url = os.getenv("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL isn't set. Put your Neon connection string in a .env file "
                 "next to this script: DATABASE_URL=postgresql://...")
    return url


def db():
    """Return the open database connection, connecting first if needed."""
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg.connect(database_url(), autocommit=True)
        try:
            register_vector(_conn)  # lets us pass numpy arrays straight in as vectors
        except psycopg.ProgrammingError:
            sys.exit("The database isn't set up yet. Run: python face_check.py setup")
    return _conn


def with_retry(work):
    """Run work(connection). If the connection had dropped (a Wi-Fi blip, or Neon pausing
    an idle database), reconnect and try once more instead of crashing the kiosk."""
    global _conn
    try:
        return work(db())
    except psycopg.OperationalError:
        print("Database connection dropped. Reconnecting...")
        _conn = None
        return work(db())


def query(sql, params=()):
    """Run one SQL statement and return the cursor, so callers can .fetchone()/.fetchall()."""
    return with_retry(lambda conn: conn.execute(sql, params))


def save_member(name, embeddings):
    """Add a member (or find the existing one) and store their embeddings, all-or-nothing."""
    def work(conn):
        with conn.transaction():  # if anything fails, nothing is saved
            member_id = conn.execute(
                """INSERT INTO members (name, consented_at) VALUES (%s, now())
                   ON CONFLICT (name) DO UPDATE
                   SET consented_at = COALESCE(members.consented_at, now())
                   RETURNING id""",
                (name,),
            ).fetchone()[0]
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO face_embeddings (member_id, embedding) VALUES (%s, %s)",
                    [(member_id, e) for e in embeddings],
                )
        return member_id
    return with_retry(work)


def best_match(embedding):
    """Ask Neon for the closest stored face. Returns (member_id, name, distance) or None."""
    return query(
        """SELECT m.id, m.name, f.embedding <=> %s AS distance
           FROM face_embeddings f JOIN members m ON m.id = f.member_id
           WHERE m.consented_at IS NOT NULL   -- skip anyone who withdrew consent in the web app
           ORDER BY distance
           LIMIT 1""",
        (embedding,),
    ).fetchone()


def latest_event():
    """Return (id, name) of the most recently created event, or exit if there isn't one."""
    row = query("SELECT id, name FROM events ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        sys.exit('No events yet. Create one: python face_check.py event "Week 1 Meeting"')
    return row


def check_in(member_id, event_id):
    """Record a check-in. Returns True if new, False if they were already checked in."""
    cur = query(
        """INSERT INTO checkins (member_id, event_id, method) VALUES (%s, %s, 'face')
           ON CONFLICT (member_id, event_id) DO NOTHING""",
        (member_id, event_id),
    )
    return cur.rowcount == 1


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
    # A numpy array (not a plain list) so pgvector sends it to Postgres as a vector
    return np.array(face["embedding"], dtype=np.float32), face["facial_area"]


# ---------- webcam helpers ----------

def open_camera():
    """Open the camera, giving it a couple of seconds to warm up."""
    cam = cv2.VideoCapture(CAMERA_INDEX)
    for _ in range(20):
        ok, _ = cam.read()
        if ok:
            print(f"Using camera {CAMERA_INDEX} (set CAMERA_INDEX in .env to change it). "
                  "If you can't see the video window, look behind VS Code.")
            return cam
        time.sleep(0.1)
    cam.release()
    sys.exit(
        f"Couldn't read from camera {CAMERA_INDEX}. Check CAMERA_INDEX in your .env file "
        "(camera_test.py shows which number is which), allow camera access in "
        "System Settings > Privacy & Security > Camera, and close other apps using the camera."
    )


def load_model():
    """Load both models up front so download problems show up now, not mid-video."""
    print("Loading face models (the first run downloads about 90 MB)...")
    DeepFace.build_model(MODEL)
    DeepFace.build_model(DETECTOR, task="face_detector")


def draw_label(img, text, x, y, color):
    """Draw text on a solid background so it's readable on any video."""
    (w, h), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
    y = max(y, h + 12)  # keep the label on screen when the face is near the top
    cv2.rectangle(img, (x, y - h - 12), (x + w + 10, y), color, -1)
    cv2.putText(img, text, (x + 5, y - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.7, WHITE, 2)


def draw_box(img, box, color):
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    cv2.rectangle(img, (x, y), (x + w, y + h), color, 2)


# ---------- commands ----------

def setup():
    with psycopg.connect(database_url(), autocommit=True) as conn:
        conn.execute(SCHEMA_FILE.read_text())
    print("Database is ready: all tables exist and are up to date.")


def seed():
    with psycopg.connect(database_url(), autocommit=True) as conn:
        conn.execute(SEED_FILE.read_text())
    print("Added the demo events and members (anything already there was kept).")


def enroll(name):
    name = name.strip()
    if not name:
        sys.exit("Please give a name, like: python face_check.py enroll \"Jace\"")
    db()  # connect now, so a database problem shows up before the photo session

    answer = input(f"Does {name} agree to store a face embedding (no photo) for check-in? [y/N] ")
    if answer.strip().lower() != "y":
        print("No consent, so nothing was captured.")
        return

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
            captured.append(embedding)
            print(f"  Captured {len(captured)}/{SHOTS_PER_PERSON}")

    cam.release()
    cv2.destroyAllWindows()

    if not captured:
        print("Nothing captured, so nothing saved.")
        return
    save_member(name, captured)
    print(f"Saved {len(captured)} embeddings for {name} in Neon.")


def create_event(name):
    event_id, held_on = query(
        "INSERT INTO events (name) VALUES (%s) RETURNING id, held_on", (name,)
    ).fetchone()
    print(f"Created event #{event_id}: {name} ({held_on}). Check-ins now go to this event.")


def identify():
    event_id, event_name = latest_event()
    members = query("SELECT count(*) FROM members").fetchone()[0]
    if members == 0:
        sys.exit('No one is enrolled yet. Run: python face_check.py enroll "Name"')
    load_model()
    cam = open_camera()
    print(f"Checking in to: {event_name}. {members} members enrolled. Threshold: {THRESHOLD}")

    last_check = 0.0
    last_no_face_note = 0.0       # so "no face" is printed every few seconds, not constantly
    result = None                 # (box, label, color) to draw until the next check
    streak_id, streak = None, 0   # who matched on recent checks, and how many times in a row
    status = ""                   # the "Welcome" message to keep showing after a check-in

    while True:
        ok, frame = cam.read()
        if not ok:
            break
        frame = cv2.flip(frame, 1)

        if time.time() - last_check >= SECONDS_BETWEEN_CHECKS:
            last_check = time.time()
            embedding, box = embed(frame)
            match = best_match(embedding) if embedding is not None else None

            if embedding is None:
                result, streak_id, streak = None, None, 0
                if time.time() - last_no_face_note >= 3:
                    last_no_face_note = time.time()
                    print("no face in view (face the camera, with light on your face)")
            elif match is None or match[2] > THRESHOLD:
                distance = match[2] if match else float("nan")
                print(f"unknown face   distance: {distance:.3f}")
                result, streak_id, streak = (box, f"Unknown ({distance:.2f})", RED), None, 0
            else:
                member_id, name, distance = match
                streak = streak + 1 if member_id == streak_id else 1
                streak_id = member_id
                print(f"{name:<15} distance: {distance:.3f}  match {min(streak, MATCHES_TO_CHECK_IN)}"
                      f"/{MATCHES_TO_CHECK_IN}")

                if streak == MATCHES_TO_CHECK_IN:
                    if check_in(member_id, event_id):
                        status = f"Welcome, {name}!"
                        print(f"  CHECKED IN: {name}")
                    else:
                        status = f"{name}: already checked in"
                        print(f"  already checked in: {name}")
                if streak >= MATCHES_TO_CHECK_IN:
                    result = (box, status, GREEN)
                else:
                    result = (box, f"{name}? ({distance:.2f})", YELLOW)

        if result:
            box, label, color = result
            draw_box(frame, box, color)
            draw_label(frame, label, box["x"], box["y"], color)
        else:
            draw_label(frame, "Looking for a face...", 10, 80, RED)
        draw_label(frame, event_name, 10, 40, GREEN)

        cv2.imshow(WINDOW, frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cam.release()
    cv2.destroyAllWindows()


def attendance():
    event_id, event_name = latest_event()
    rows = query(
        """SELECT m.name, c.method, c.checked_in_at
           FROM checkins c JOIN members m ON m.id = c.member_id
           WHERE c.event_id = %s
           ORDER BY c.checked_in_at""",
        (event_id,),
    ).fetchall()
    print(f"{event_name}: {len(rows)} checked in")
    for name, method, at in rows:
        print(f"  {at.astimezone():%I:%M %p}  {name:<20} ({method})")


def list_members():
    rows = query(
        """SELECT m.name,
                  (SELECT count(*) FROM face_embeddings f WHERE f.member_id = m.id),
                  (SELECT count(*) FROM checkins c WHERE c.member_id = m.id)
           FROM members m
           ORDER BY m.name"""
    ).fetchall()
    if not rows:
        print("No one enrolled.")
    for name, faces, events in rows:
        plural = "" if events == 1 else "s"
        print(f"{name:<20} {faces} embeddings, {events} event{plural} attended")


def delete(name):
    cur = query("DELETE FROM members WHERE name = %s RETURNING name", (name,))
    if cur.fetchone() is None:
        print(f"No member named {name!r}. Run 'python face_check.py list' to see exact names.")
    else:
        print(f"Deleted {name} along with all their face data and check-ins.")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["setup"]:
        setup()
    elif args == ["seed"]:
        seed()
    elif args[:1] == ["enroll"] and len(args) == 2:
        enroll(args[1])
    elif args[:1] == ["event"] and len(args) == 2:
        create_event(args[1])
    elif args == ["identify"]:
        identify()
    elif args == ["attendance"]:
        attendance()
    elif args == ["list"]:
        list_members()
    elif args[:1] == ["delete"] and len(args) == 2:
        delete(args[1])
    else:
        if args:
            print(f"Didn't recognize: python face_check.py {' '.join(args)}")
            print("Each command starts with a word like enroll, event or identify. See below.")
        print(__doc__)
