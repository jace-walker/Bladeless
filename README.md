# Blade face check-in

Face check-in for club events. Members opt in and enroll five shots of their face. At an event, a laptop or phone at the door checks them in when it recognizes them. Organizers manage members, dues, consent, and attendance from an admin page.

- **Hosting:** the Vercel deployment runs a Flask API (`app.py`).
- **Data:** everything is stored in **Neon Postgres**, and **pgvector** does the face matching inside the database.
- **Face model:** Facenet512 via DeepFace. Its weights load from the **Hugging Face Hub**.

## How it fits together

```
Browser pages (public/)          Vercel function (app.py)                 Neon Postgres
 kiosk / profile / admin   --->   /api/... endpoints                --->   members, events, checkins,
 attendee phone page              face_engine.py: snapshot -> embedding    face_embeddings (pgvector),
                                  db.py: all the SQL                       kiosk settings + latest result
```

- **Photos are never stored.** A camera snapshot is turned into a 512-number embedding in memory, then thrown away.
- **Matching happens in the database.** One query with pgvector's `<=>` (cosine distance) operator finds the closest consenting member. A distance under the strictness setting (default 0.30) checks them in.
- **Consent comes first.** Consent is required before capture. Withdrawing consent takes a member out of matching right away, and *Delete face data* removes their embeddings while keeping attendance history.

| File | What it does |
| --- | --- |
| `app.py` | The web API that runs on Vercel (Flask) |
| `db.py` | All database queries: members, dues, consent, events, check-ins, kiosk settings |
| `face_engine.py` | Snapshot → embedding with DeepFace. Downloads the model weights from Hugging Face |
| `schema.sql` | Creates and upgrades the Neon tables. Safe to run again |
| `seed.sql` | Demo events and made-up members for trying the app |
| `public/` | The web pages, which Vercel serves directly: `index.html` (profile, kiosk, admin), `guest.html` (attendee phone), `pair.html` |
| `face_check.py` | Terminal tool: enroll and check in with a webcam, using the same Neon database |
| `tools/import_local_data.py` | Copies `faces.json` and `attendance.sqlite3` from the original local demo into Neon |
| `tests/test_api.py` | API tests |

## Deploying on Vercel

1. **Set up the database (once).** From a laptop with this repo and a `.env` containing your Neon `DATABASE_URL`, run:
   ```bash
   python face_check.py setup   # creates or upgrades the tables
   python face_check.py seed    # optional: demo events and members
   ```
2. **Bring over existing face data (optional).** On the computer that ran the old local demo, run:
   ```bash
   python tools/import_local_data.py --faces faces.json --sqlite attendance.sqlite3
   ```
3. **Add environment variables.** In the Vercel project, go to **Settings → Environment Variables** and add:

   | Name | Value |
   | --- | --- |
   | `DATABASE_URL` | Neon's **pooled** connection string (in Neon's **Connect** dialog, turn **Connection pooling** on) |
   | `ADMIN_KEY` | A long random key for the organizer's computer |
   | `PHONE_KEY` | A different key for the attendee phone |

   To make a key, run `python -c "import secrets; print(secrets.token_urlsafe(16))"`. Never commit these values.
4. **Check the framework setting.** Under **Settings → Build and Deployment**, the framework preset should be **Flask**. Pick it if Vercel didn't detect it. `pyproject.toml` tells Vercel the app is `app:app`.
5. **Redeploy.** Push to `main`, or use **Deployments → ⋯ → Redeploy**. Environment variable changes only apply after a redeploy.
6. **Check the deployment.** Open `https://<your-project>.vercel.app/api/health`. It should show `"ok": true` and `"adminKeySet": true`.
7. **Pair your devices.** Open `/pair.html` and enter `ADMIN_KEY` on the organizer's computer, or `PHONE_KEY` on the attendee phone.

**Size and speed:** the deployment is about 450 MB, under Vercel's 500 MB limit for Python. If a future change pushes it over, add the environment variable `VERCEL_SUPPORT_LARGE_FUNCTIONS=1`. After a quiet period, the first request takes about 10 seconds while the face model loads; after that, each check takes about 0.2 seconds.

## The pages (`public/`)

The pages come from the local demo, and they call the same endpoints as before. What changed when they moved to Vercel:

- **Frames are checked right away.** The computer kiosk and the attendee phone send a frame to `POST /api/camera` about once a second (JPEG, at most 1024 px on each side and 250 KB). The server checks each one immediately and returns the result. The admin page and `/api/display` show the latest result.
- **No phone preview or phone enrollment.** The old server relayed the phone's camera to the organizer's computer. Vercel can't hold frames between requests, so `GET /api/camera/frame` always returns 204. Enroll with the computer's camera. Phone check-in at the kiosk still works.
- **Pairing.** Pages load without a key, and any API call that returns 403 sends you to `/pair.html`.
- **Accurate wording.** Text about the local server and Cloudflare tunnel was updated, including the consent text, so it describes where frames now go.
- **IDs are strings.** Member ids are numbers sent as strings (`"3"`), and events use their short id (`"intro-ai"`). Manual check-in also accepts a member code (`KH-0001`) or QR text `member:<id or code>`.

## Running locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
flask --app app run --debug        # http://127.0.0.1:5000, serves public/ too
```

Locally, if `ADMIN_KEY` isn't set, everything is open (handy for local testing). On Vercel, `ADMIN_KEY` is required.

The terminal tool also works with the same database. See the comment at the top of `face_check.py`. It needs the OpenCV build that can open windows (see the note in `requirements.txt`).

## Tests

The tests **erase every table** in the database they use, so point them at a separate test database, such as a Neon branch made for testing:

```bash
pip install pytest
TEST_DATABASE_URL=postgresql://...test database... python -m pytest tests -q
```

The face model is replaced with a stand-in, so the tests take a few seconds and need no camera.

## API

All endpoints return JSON. Every endpoint except `/api/pair` and `/api/health` needs a paired device. The phone key can only use `/api/display` and `/api/camera`.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Deployment check (database reachable, admin key set) |
| `POST /api/pair` `{key}` | Pair this browser (sets a cookie) |
| `GET /api/state` | Members, events, attendance, recent results, settings, latest match |
| `GET /api/display` | Latest result status for the attendee screen |
| `POST /api/camera` | A JPEG frame. Recognizes it if the kiosk is running |
| `POST /api/settings` | `{eventId, threshold, requireLiveness, running}` (any subset) |
| `POST /api/members/add` `{name}` | Add a member |
| `POST /api/members/consent` `{memberId, consent}` | Give or withdraw consent |
| `POST /api/members/dues` `{memberId, paid}` | Toggle dues |
| `POST /api/enroll` `{memberId, image}` | Base64 JPEG. Stores one embedding |
| `POST /api/faces/delete` `{memberId}` | Delete face data and withdraw consent |
| `POST /api/checkins` `{memberId, eventId}` | Manual or QR check-in |
| `POST /api/reset` `{confirm: true}` | Clear new attendance and recent results |

Possible check-in results: `checked_in`, `already_checked_in`, `dues_required`, `not_enrolled`, `member_not_found`, `unknown`, `liveness_failed`, `camera_error`, `idle`.
