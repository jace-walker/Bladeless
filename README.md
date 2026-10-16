# Blade face check-in

Face check-in for club events. Members opt in and enroll five shots of their face. At an event, a laptop at the door checks them in when it recognizes them, several people at once if they arrive together. Organizers manage members, dues, consent, and attendance from an admin page.

- **Hosting:** the Vercel deployment runs a Flask API (`app.py`).
- **Data:** everything is stored in **Neon Postgres**, and **pgvector** does the face matching inside the database.
- **Face model:** OpenCV's YuNet detector finds the face, and Facenet512 (via DeepFace) turns it into numbers. Facenet512's weights load from the **Hugging Face Hub**.
- **Access:** open by default, so anyone with the link can try the demo. Setting `ADMIN_KEY` turns pairing back on. With `CRON_SECRET` set, the demo resets itself every night.
- **Pages:** a Blade-style homepage (`/`) with the demo's Check-in and About sections; the check-in app itself is `/checkin.html`.

## How it fits together

```
Browser pages (public/)          Vercel function (app.py)                 Neon Postgres
 check in / enroll / admin --->   /api/... endpoints                --->   members, events, checkins,
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
| `public/` | The web pages, which Vercel serves directly: `index.html` (the Blade-style homepage), `checkin.html` (check in, add my face, organizer), `guest.html` (attendee screen, and phone check-in when it's on), `pair.html` (only used when `ADMIN_KEY` is set) |
| `vercel.json` | Schedules the nightly demo reset (Vercel Cron Jobs) |
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
3. **Add environment variables.** In the Vercel project, go to **Settings → Environment Variables**. Only `DATABASE_URL` is required:

   | Name | Value |
   | --- | --- |
   | `DATABASE_URL` | Required. Neon's **pooled** connection string (in Neon's **Connect** dialog, turn **Connection pooling** on) |
   | `ADMIN_KEY` | Optional. Leave it unset for an open demo. Set it to a long random key to make every device pair first |
   | `PHONE_CHECKIN` | Optional, off by default. Set to `on` to allow check-in with the attendee phone's camera |
   | `PHONE_KEY` | Optional. A key for the attendee phone, used only when `ADMIN_KEY` is set and `PHONE_CHECKIN` is `on` |
   | `CRON_SECRET` | Optional. Turns on the nightly reset. Any long random value; Vercel sends it with each scheduled call |
   | `yunet_score_threshold` | Optional, default `0.9`. How sure the face detector must be. Try `0.8` if it misses faces in a dark room |

   To make a key, run `python -c "import secrets; print(secrets.token_urlsafe(16))"`. Never commit these values.
4. **Check the framework setting.** Under **Settings → Build and Deployment**, the framework preset should be **Flask**. Pick it if Vercel didn't detect it. `pyproject.toml` tells Vercel the app is `app:app`.
5. **Redeploy.** Push to `main`, or use **Deployments → ⋯ → Redeploy**. Environment variable changes only apply after a redeploy.
6. **Check the deployment.** Open `https://<your-project>.vercel.app/api/health`. It should show `"ok": true`. For an open demo, it also shows `"openAccess": true`.
7. **Pair your devices (only if `ADMIN_KEY` is set).** Open `/pair.html` and enter `ADMIN_KEY` on the organizer's computer, or `PHONE_KEY` on the attendee phone.

**The nightly reset.** `vercel.json` asks Vercel to call `/api/cron/reset-demo` once a day at 08:00 UTC (about 4 AM Eastern; on the free plan it runs at some point within that hour, and only on the production deployment). The call deletes every member, face, check-in and event visitors added, then restores the made-up members and events from `seed.sql`. It only runs when `CRON_SECRET` is set; anyone calling it without that secret is refused.

**After pulling an update that changes `schema.sql`,** run `python face_check.py setup` against your database (or paste `schema.sql` into Neon's SQL Editor). It's safe to run again and only adds what's missing, such as the index that keeps matching fast as membership grows.

**Switching face detectors changes the embeddings.** Faces enrolled with the old Haar detector (before YuNet) match less reliably, so delete their face data and enroll them again.

**Size and speed:** the deployment is about 450 MB, under Vercel's 500 MB limit for Python. If a future change pushes it over, add the environment variable `VERCEL_SUPPORT_LARGE_FUNCTIONS=1`. After a quiet period, the first request takes about 10 seconds while the face model loads; after that, each check takes about 0.2 seconds.

### Testing previews against a copy of the database

Every branch you push gets its own Vercel preview. To keep previews from touching real attendance, give them a [Neon branch](https://neon.com/docs/introduction/branching) — a copy of the database:

1. In the Neon console, open **Branches → Create branch**. Name it `preview` and create it from `main`. It starts as a copy of your current data.
2. Open the `preview` branch, click **Connect**, turn **Connection pooling** on, and copy the connection string.
3. In Vercel, go to **Settings → Environment Variables**. Edit the existing `DATABASE_URL` so it applies to **Production** only. Then add a second `DATABASE_URL` with the `preview` branch's string, for **Preview** only.
4. Redeploy the preview. Production keeps using `main`, and previews use the copy.

To refresh the copy with current data later, open the `preview` branch in Neon and choose **Reset from parent**. If a change edits `schema.sql`, run it on the `preview` branch first to try the upgrade safely.

## The pages (`public/`)

`index.html` is a Blade-style homepage. It has Blade's member sections (Dashboard, Events, Dues, Forms, Settings), each marked as not part of the demo, plus the two that work here: **Check-in**, which links into the app, and **About**, a short version of this README.

`checkin.html` is the app, written so anyone can use it without help:

- **Check in:** one big “Start the camera” button. The page loads the face model while the camera starts, then sends a snapshot about every half second. Each person in view gets a box and their first name, and the result is in plain words (“Hi, Sam! You're checked in.”). Up to four people near the camera are checked in from the same snapshot; people much farther back than the nearest face are ignored.
- **Add my face:** type your name, say OK to the consent text, and take five pictures, each with a simple prompt (“Turn your head a little to your left”). “Check in now” on the last screen starts the camera straight away.
- **Organizer:** live results, the event, match strictness, liveness, manual or QR check-in, members (dues, face data, remove), events (create, delete before anyone checks in), recent check-ins, and reset.

The pages started as the local demo's and call the same endpoints. What changed when they moved to Vercel:

- **Frames are checked right away.** The computer kiosk and the attendee phone send a frame to `POST /api/camera` about every half second (JPEG, at most 1024 px on each side and 250 KB). The server checks each one immediately and returns the result. The admin page and `/api/display` show the latest result.
- **No phone preview or phone enrollment.** The old server relayed the phone's camera to the organizer's computer. Vercel can't hold frames between requests, so `GET /api/camera/frame` always returns 204. Enroll with the computer's camera. Phone check-in is off unless `PHONE_CHECKIN` is `on`; the attendee screen still shows results either way.
- **Pairing.** Only when `ADMIN_KEY` is set. Pages load without a key, and any API call that returns 403 sends you to `/pair.html`.
- **Accurate wording.** Text about the local server and Cloudflare tunnel was updated, including the consent text, so it describes where frames now go.
- **IDs are strings.** Member ids are numbers sent as strings (`"3"`), and events use their short id (`"intro-ai"`). Manual check-in also accepts a member code (`KH-0001`) or QR text `member:<id or code>`.

## Running locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
flask --app app run --debug        # http://127.0.0.1:5000, serves public/ too
```

If `ADMIN_KEY` isn't set, everything is open, locally and on Vercel.

The terminal tool also works with the same database. See the comment at the top of `face_check.py`. It needs the OpenCV build that can open windows (see the note in `requirements.txt`).

## Tests

The tests **erase every table** in the database they use, so point them at a separate test database, such as a Neon branch made for testing:

```bash
pip install pytest
TEST_DATABASE_URL=postgresql://...test database... python -m pytest tests -q
```

The face model is replaced with a stand-in, so the tests take a few seconds and need no camera.

## API

All endpoints return JSON. With no `ADMIN_KEY` set, every endpoint is open. With one set, every endpoint except `/api/pair` and `/api/health` needs a paired device, and the phone key (when phone check-in is on) can only use `/api/display` and `/api/camera`.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Deployment check (database reachable, open access or keys, phone check-in on or off) |
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
| `POST /api/members/remove` `{memberId}` | Remove a member, along with their face data and attendance |
| `POST /api/events/add` `{name, date, time?, location?, points?, duesOnly?}` | Create an event (`date` as `YYYY-MM-DD`, `time` as `HH:MM`) |
| `POST /api/events/delete` `{eventId}` | Delete an event nobody has checked in to |
| `POST /api/warmup` | Load the face model now, so the first check-in doesn't wait for it |
| `GET /api/cron/reset-demo` | The nightly reset. Needs `Authorization: Bearer <CRON_SECRET>` |

`POST /api/camera` returns the nearest face's result, plus `faces`: one result per face near the camera, each with its `box` (`x`, `y`, `w`, `h` in the frame's pixels).

Possible check-in results: `checked_in`, `already_checked_in`, `dues_required`, `not_enrolled`, `member_not_found`, `unknown`, `liveness_failed`, `camera_error`, `idle`.
