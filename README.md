# Face Check-In

Webcam check-in for CS club events. Members enroll once, then walk up to a laptop at the door and get checked in by face. Neon Postgres stores the members, events and check-ins, and pgvector does the face matching inside the database.

## How it works

1. **Enroll:** A member agrees to enrollment, then the webcam takes 5 shots. A face model (Facenet512 via DeepFace) turns each shot into an *embedding*, a list of 512 numbers that summarizes a face. Only the embeddings are stored. Photos are never saved.
2. **Check in:** During an event, the kiosk embeds each face it sees and asks Neon for the closest stored embedding using pgvector's `<=>` (cosine distance) operator. A distance under 0.30 that holds for two checks in a row records a check-in.
3. **Privacy:** Enrollment requires consent. Deleting a member removes all their face data and check-ins (`ON DELETE CASCADE`).

## Setup

You need Python 3.12 or 3.13 and a webcam.

```bash
git clone <this repo's URL>
cd <repo folder>
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip uninstall -y opencv-contrib-python-headless
pip install opencv-contrib-python==5.0.0.93
```

The last two lines matter. DeepFace installs a version of OpenCV that can't open windows, so you swap in the full one.

Next, copy `.env.example` to a new file named `.env` and fill it in:

- **`DATABASE_URL`:** Ask a teammate for the Neon connection string. Never commit `.env`.
- **`CAMERA_INDEX`:** Usually `0`. If the wrong camera opens (an iPhone on a Mac, for example), run `python camera_test.py` to see which number is which.

The first run downloads about 90 MB of model weights.

## Commands

```bash
python face_check.py setup                  # create the tables (safe to run again)
python face_check.py enroll "Jace"          # enroll a member (5 shots, press SPACE)
python face_check.py event "Week 3 Meeting" # start a new event
python face_check.py identify               # live check-in for the latest event (Q quits)
python face_check.py attendance             # who checked in to the latest event
python face_check.py list                   # everyone enrolled
python face_check.py delete "Jace"          # remove a member and all their data
```

Click the video window before pressing keys.

On macOS, allow camera access for your terminal or VS Code in System Settings → Privacy & Security → Camera, then fully quit and reopen it.

## Project structure

| File | What it does |
| --- | --- |
| `face_check.py` | The kiosk: enrollment, live recognition, check-ins |
| `schema.sql` | Database tables: members, face_embeddings, events, checkins |
| `camera_test.py` | Shows each camera's view, to find the right `CAMERA_INDEX` |
| `.env.example` | Template for your local `.env` |

## Team workflow

- **Before you start:** Pull the latest changes (VS Code: **Sync Changes**).
- **New features:** Work on a branch and open a pull request, so two people don't edit the same lines at once.
- **Off-limits for commits:** `.env`, `.venv/`, and `faces.json`. `.gitignore` already blocks them.

## Roadmap

- [x] Webcam enrollment and recognition
- [x] Neon storage with pgvector matching and check-ins
- [ ] Officer dashboard (JavaScript on Vercel, reading from Neon)
- [ ] Kiosk and dashboard UI designed in Figma
- [ ] "Ask your attendance": plain-English questions turned into SQL by an open-source model via Hugging Face
- [ ] Liveness check, so a photo can't check someone in
- [ ] QR code and manual check-in fallback
