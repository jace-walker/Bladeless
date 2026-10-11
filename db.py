"""Everything the web app stores lives in Neon. This module holds all of the SQL.

It replaces the original local demo's storage (SQLite + faces.json on one laptop).
Every function takes an open connection, so app.py can use one connection per request.

IDs: the database uses integer ids, but the API always sends ids as strings
(events use their slug, like 'intro-ai', when they have one), matching what the
original pages expect.
"""

import datetime as dt
import json
import os
import re
import uuid
from contextlib import contextmanager

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

MIN_SAMPLES = 5        # shots needed before a member can be recognized
MAX_SAMPLES = 20
MAX_MEMBERS = 200
LOG_LIMIT = 100        # recent results kept for the admin page
LOG_REPEAT_SECONDS = 10
DISPLAY_TTL_SECONDS = 8
SETTING_KEYS = {"eventId", "threshold", "requireLiveness", "running"}


class ApiError(ValueError):
    """A problem with the request; the message is shown to the user."""


@contextmanager
def connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    # prepare_threshold=None: Neon's pooled connections can't keep prepared statements.
    conn = psycopg.connect(url, autocommit=True, prepare_threshold=None)
    try:
        register_vector(conn)
        yield conn
    finally:
        conn.close()


# ---------- events ----------

EVENT_COLUMNS = "id, slug, name, held_on, date_label, location, points, audience"


def _event_id(row):
    return row["slug"] or str(row["id"])


def _event_dict(row):
    return dict(
        id=_event_id(row),
        name=row["name"],
        date=row["date_label"] or _date_label(row["held_on"], None),
        heldOn=row["held_on"].isoformat(),
        location=row["location"] or "",
        points=row["points"],
        audience=row["audience"],
        checkins=row.get("checkins", 0),
        roleIds=[],
    )


def _date_label(day, start):
    """'Oct 27, 2026' or 'Oct 27, 2026, 6:30 PM' (same style as the seeded events)."""
    label = f"{day:%b} {day.day}, {day.year}"
    if start:
        hour = start.hour % 12 or 12
        label += f", {hour}:{start.minute:02d} {'AM' if start.hour < 12 else 'PM'}"
    return label


def _rows(cur):
    names = [c.name for c in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _row(cur):
    rows = _rows(cur)
    return rows[0] if rows else None


def list_events(conn):
    cur = conn.execute(
        f"""SELECT {EVENT_COLUMNS},
                   (SELECT count(*) FROM checkins c WHERE c.event_id = events.id) AS checkins
            FROM events ORDER BY held_on, id"""
    )
    return [_event_dict(r) for r in _rows(cur)]


EVENT_FIELDS = {"name", "date", "time", "location", "points", "duesOnly"}


def _slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-") or "event"
    return "event-" + slug if slug.isdigit() else slug  # never confusable with a numeric id


def add_event(conn, payload):
    """Create an event from the admin form. Returns the new event."""
    if set(payload) - EVENT_FIELDS:
        raise ApiError("Unknown event field")
    name = payload.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ApiError("Enter an event name of 1–100 characters")
    name = name.strip()
    try:
        day = dt.date.fromisoformat(payload.get("date") or "")
    except (TypeError, ValueError):
        raise ApiError("Choose a date for the event")
    start = None
    if payload.get("time"):
        try:
            start = dt.time.fromisoformat(payload["time"])
        except (TypeError, ValueError):
            raise ApiError("Enter the start time as hours and minutes, like 17:30")
    location = payload.get("location") or ""
    if not isinstance(location, str) or len(location.strip()) > 80:
        raise ApiError("Keep the location under 80 characters")
    points = payload.get("points", 10)
    if isinstance(points, bool) or not isinstance(points, int) or not 0 <= points <= 1000:
        raise ApiError("Points must be a whole number from 0 to 1000")
    dues_only = payload.get("duesOnly", False)
    if type(dues_only) is not bool:
        raise ApiError("duesOnly must be true or false")

    base = slug = _slugify(name)
    suffix = 2
    while conn.execute("SELECT 1 FROM events WHERE slug = %s", (slug,)).fetchone():
        slug, suffix = f"{base}-{suffix}", suffix + 1
    row = _row(conn.execute(
        f"""INSERT INTO events (slug, name, held_on, date_label, location, points, audience)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING {EVENT_COLUMNS}""",
        (slug, name, day, _date_label(day, start), location.strip() or None, points,
         "dues" if dues_only else "public"),
    ))
    return _event_dict(row)


def delete_event(conn, ident):
    """Delete an event nobody has checked in to yet, unless the running kiosk is using it."""
    event = find_event(conn, ident)
    if event is None:
        raise ApiError("Event not found")
    with conn.transaction():
        if conn.execute("SELECT 1 FROM checkins WHERE event_id = %s LIMIT 1", (event["id"],)).fetchone():
            raise ApiError("People have checked in to this event, so it can't be deleted.")
        kiosk = conn.execute("SELECT event_id, running FROM kiosk_settings WHERE id = 1").fetchone()
        if kiosk and kiosk[0] == event["id"] and kiosk[1]:
            raise ApiError("Stop the kiosk before deleting the event it's using.")
        conn.execute("DELETE FROM events WHERE id = %s", (event["id"],))
    return dict(status="deleted")


def find_event(conn, ident):
    if ident is None:
        return None
    ident = str(ident)
    return _row(conn.execute(
        f"SELECT {EVENT_COLUMNS} FROM events WHERE slug = %s OR id::text = %s LIMIT 1",
        (ident, ident),
    ))


# ---------- members ----------

def _member_dict(row):
    return dict(
        id=str(row["id"]),
        name=row["name"],
        face_name=row["name"],
        displayId=row["display_id"] or "",
        paid=row["paid"],
        consent=row["consent"],
        samples=row["samples"],
        enrolled=row["consent"] and row["samples"] >= MIN_SAMPLES,
        eventsAttended=row["base_events"] + row["attended"],
        roleIds=[],
        epoch=row["epoch"],
    )


MEMBER_QUERY = """
    SELECT m.id, m.name, m.display_id, m.paid, m.base_events, m.epoch,
           m.consented_at IS NOT NULL AS consent,
           (SELECT count(*) FROM face_embeddings f WHERE f.member_id = m.id) AS samples,
           (SELECT count(*) FROM checkins c WHERE c.member_id = m.id) AS attended
    FROM members m
"""


def list_members(conn):
    return [_member_dict(r) for r in _rows(conn.execute(MEMBER_QUERY + " ORDER BY m.id"))]


def find_member(conn, ident):
    """Look a member up by id, member code (KH-0001) or QR text 'member:<id or code>'."""
    if not isinstance(ident, (str, int)) or isinstance(ident, bool):
        return None
    ident = str(ident).strip()
    if ident.lower().startswith("member:"):
        ident = ident[len("member:"):].strip()
    if not ident:
        return None
    return _row(conn.execute(
        MEMBER_QUERY + " WHERE m.id::text = %s OR upper(m.display_id) = upper(%s) LIMIT 1",
        (ident, ident),
    ))


def member_or_error(conn, ident):
    row = find_member(conn, ident)
    if row is None:
        raise ApiError("Member not found")
    return row


def add_member(conn, name):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ApiError("Enter a name of 1–80 characters")
    name = name.strip()
    if conn.execute("SELECT count(*) FROM members").fetchone()[0] >= MAX_MEMBERS:
        raise ApiError("Member limit reached")
    if conn.execute("SELECT 1 FROM members WHERE lower(name) = lower(%s)", (name,)).fetchone():
        raise ApiError("That name already exists")
    code = "KH-" + uuid.uuid4().hex[:6].upper()
    member_id = conn.execute(
        "INSERT INTO members (name, display_id) VALUES (%s, %s) RETURNING id", (name, code)
    ).fetchone()[0]
    return _member_dict(find_member(conn, member_id))


def remove_member(conn, ident):
    """Delete a member entirely: their face data and attendance go with them (ON DELETE CASCADE)."""
    row = member_or_error(conn, ident)
    conn.execute("DELETE FROM members WHERE id = %s", (row["id"],))
    return dict(status="removed", name=row["name"])


def set_consent(conn, ident, value):
    if type(value) is not bool:
        raise ApiError("Consent must be true or false")
    row = member_or_error(conn, ident)
    conn.execute(
        """UPDATE members
           SET consented_at = CASE WHEN %s THEN COALESCE(consented_at, now()) END,
               epoch = epoch + 1
           WHERE id = %s""",
        (value, row["id"]),
    )
    return dict(status="saved")


def set_paid(conn, ident, value):
    if type(value) is not bool:
        raise ApiError("Paid must be true or false")
    row = member_or_error(conn, ident)
    conn.execute("UPDATE members SET paid = %s WHERE id = %s", (value, row["id"]))
    return dict(status="saved")


def delete_face_data(conn, ident):
    """Delete all embeddings and revoke consent. Attendance history is kept."""
    row = member_or_error(conn, ident)
    with conn.transaction():
        conn.execute("DELETE FROM face_embeddings WHERE member_id = %s", (row["id"],))
        conn.execute(
            "UPDATE members SET consented_at = NULL, epoch = epoch + 1 WHERE id = %s", (row["id"],)
        )
    return dict(status="deleted")


def add_embedding(conn, member_id, embedding, epoch):
    """Store one enrollment shot, as long as consent hasn't changed since the capture began."""
    with conn.transaction():
        row = _row(conn.execute(
            "SELECT consented_at IS NOT NULL AS consent, epoch FROM members WHERE id = %s FOR UPDATE",
            (member_id,),
        ))
        if row is None:
            raise ApiError("Member not found")
        if not row["consent"] or row["epoch"] != epoch:
            raise ApiError("Consent changed; capture again")
        count = conn.execute(
            "SELECT count(*) FROM face_embeddings WHERE member_id = %s", (member_id,)
        ).fetchone()[0]
        if count >= MAX_SAMPLES:
            raise ApiError(f"{MAX_SAMPLES}-sample limit reached; delete face data to re-enroll")
        conn.execute(
            "INSERT INTO face_embeddings (member_id, embedding) VALUES (%s, %s)",
            (member_id, np.asarray(embedding, dtype=np.float32)),
        )
    samples = count + 1
    return dict(status="captured", samples=samples, enrolled=samples >= MIN_SAMPLES)


def best_match(conn, embedding):
    """Ask pgvector for the closest face among members who consented and have
    enough shots. Returns (member_id, epoch, distance) or None."""
    return conn.execute(
        """SELECT m.id, m.epoch, f.embedding <=> %s AS distance
           FROM face_embeddings f JOIN members m ON m.id = f.member_id
           WHERE m.consented_at IS NOT NULL
             AND (SELECT count(*) FROM face_embeddings f2 WHERE f2.member_id = m.id) >= %s
           ORDER BY distance
           LIMIT 1""",
        (np.asarray(embedding, dtype=np.float32), MIN_SAMPLES),
    ).fetchone()


# ---------- kiosk settings ----------

def get_settings(conn):
    row = _row(conn.execute(
        """SELECT s.threshold, s.require_liveness, s.running, e.id AS event_pk, e.slug
           FROM kiosk_settings s LEFT JOIN events e ON e.id = s.event_id
           WHERE s.id = 1"""
    ))
    if row is None:
        raise RuntimeError("Database isn't set up. Run: python face_check.py setup")
    event_id = row["slug"] or (str(row["event_pk"]) if row["event_pk"] else None)
    if event_id is None:  # no event chosen yet: default to the first one, if any
        events = list_events(conn)
        event_id = events[0]["id"] if events else None
    return dict(
        eventId=event_id,
        threshold=round(float(row["threshold"]), 2),
        requireLiveness=row["require_liveness"],
        running=row["running"],
    )


def update_settings(conn, patch):
    if not isinstance(patch, dict) or set(patch) - SETTING_KEYS:
        raise ApiError("Unknown setting")
    s = get_settings(conn)
    s.update(patch)
    event = find_event(conn, s["eventId"])
    if event is None:
        raise ApiError("Unknown event")
    threshold = s["threshold"]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0.1 <= threshold <= 0.6:
        raise ApiError("Threshold must be between 0.10 and 0.60")
    if any(type(s[k]) is not bool for k in ("running", "requireLiveness")):
        raise ApiError("Invalid settings")
    conn.execute(
        """UPDATE kiosk_settings
           SET event_id = %s, threshold = %s, require_liveness = %s, running = %s
           WHERE id = 1""",
        (event["id"], threshold, s["requireLiveness"], s["running"]),
    )
    return get_settings(conn)


# ---------- check-ins ----------

def checkin(conn, ident, event_ident, method="manual", distance=None, epoch=None):
    """Record attendance, applying the same rules as the local demo:
    consent for face check-ins, one check-in per event, and dues for dues-only events."""
    event = find_event(conn, event_ident)
    if event is None:
        raise ApiError("Event not found")
    event_id = _event_id(event)
    member = find_member(conn, ident)
    if member is None:
        return dict(status="member_not_found", method=method, eventId=event_id)

    data = dict(
        member=dict(id=str(member["id"]), name=member["name"],
                    displayId=member["display_id"] or "", paid=member["paid"]),
        method=method,
        eventId=event_id,
    )
    if distance is not None:
        data["distance"] = round(float(distance), 3)

    with conn.transaction():
        current = _row(conn.execute(
            "SELECT paid, consented_at IS NOT NULL AS consent, epoch FROM members WHERE id = %s FOR UPDATE",
            (member["id"],),
        ))
        settings = get_settings(conn)
        if method == "face" and (not settings["running"] or settings["eventId"] != event_id):
            data["status"] = "idle"
        elif method == "face" and (not current["consent"] or (epoch is not None and current["epoch"] != epoch)):
            data["status"] = "not_enrolled"
        elif conn.execute(
            "SELECT 1 FROM checkins WHERE member_id = %s AND event_id = %s", (member["id"], event["id"])
        ).fetchone():
            data["status"] = "already_checked_in"
        elif event["audience"] == "dues" and not current["paid"]:
            data["status"] = "dues_required"
        elif event["audience"] == "roles":
            data["status"] = "role_required"
        else:
            inserted = conn.execute(
                """INSERT INTO checkins (member_id, event_id, method, distance) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (member_id, event_id) DO NOTHING RETURNING 1""",
                (member["id"], event["id"], method, distance),
            ).fetchone()
            if inserted:
                data.update(status="checked_in", pointsAwarded=event["points"], additionalAttendance=False)
            else:
                data["status"] = "already_checked_in"
    record(conn, data)
    return data


def record(conn, data):
    """Add a result to the admin page's recent list, skipping exact repeats within 10 seconds."""
    if data.get("status") == "idle":
        return
    member_id = (data.get("member") or {}).get("id")
    repeat = conn.execute(
        """SELECT 1 FROM checkin_log
           WHERE result->>'status' = %s
             AND COALESCE(result->'member'->>'id', '') = %s
             AND COALESCE(result->>'eventId', '') = %s
             AND created_at > now() - make_interval(secs => %s)
           LIMIT 1""",
        (data.get("status"), member_id or "", data.get("eventId") or "", LOG_REPEAT_SECONDS),
    ).fetchone()
    if repeat:
        return
    conn.execute("INSERT INTO checkin_log (result) VALUES (%s)", (json.dumps(data),))
    conn.execute(
        "DELETE FROM checkin_log WHERE id NOT IN (SELECT id FROM checkin_log ORDER BY id DESC LIMIT %s)",
        (LOG_LIMIT,),
    )


def reset_attendance(conn):
    """Stop the kiosk and clear new attendance and recent results.
    Members, dues, face data and the earlier-event counts are kept."""
    with conn.transaction():
        conn.execute("UPDATE kiosk_settings SET running = false WHERE id = 1")
        conn.execute("DELETE FROM checkins")
        conn.execute("DELETE FROM checkin_log")
    publish(conn, dict(status="idle"))
    return dict(status="reset")


# ---------- what the screens show ----------

def publish(conn, data):
    conn.execute(
        "UPDATE kiosk_status SET result = %s, updated_at = now() WHERE id = 1", (json.dumps(data),)
    )


def current_display(conn, private=False):
    """The latest result. Attendees only see the status; admins see the details."""
    row = conn.execute(
        "SELECT result, extract(epoch FROM now() - updated_at) FROM kiosk_status WHERE id = 1"
    ).fetchone()
    if row is None or row[1] > DISPLAY_TTL_SECONDS:
        return dict(status="idle")
    result = row[0]
    return result if private else dict(status=result.get("status", "idle"))


def claim_frame_slot(conn, min_seconds):
    """True if enough time has passed since the last processed frame.
    Lets the kiosk skip frames that arrive faster than they can be checked."""
    return conn.execute(
        """UPDATE kiosk_status SET last_frame_at = now()
           WHERE id = 1 AND last_frame_at < now() - make_interval(secs => %s)
           RETURNING 1""",
        (min_seconds,),
    ).fetchone() is not None


def state(conn):
    """Everything the admin page needs, in the same shape as the local demo."""
    attendance = [
        dict(member_id=str(r["member_id"]), event_id=r["slug"] or str(r["event_id"]),
             created_at=r["checked_in_at"].strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")
        for r in _rows(conn.execute(
            """SELECT c.member_id, c.event_id, e.slug, c.checked_in_at AT TIME ZONE 'UTC' AS checked_in_at
               FROM checkins c JOIN events e ON e.id = c.event_id
               ORDER BY c.checked_in_at DESC"""
        ))
    ]
    recent = [
        dict(r["result"], createdAt=r["created_at"])
        for r in _rows(conn.execute(
            "SELECT result, extract(epoch FROM created_at)::float AS created_at "
            "FROM checkin_log ORDER BY id DESC LIMIT 50"
        ))
    ]
    return dict(
        members=list_members(conn),
        events=list_events(conn),
        attendance=attendance,
        recent=recent,
        settings=get_settings(conn),
    )
