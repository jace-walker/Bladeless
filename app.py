"""Blade face check-in API, deployed on Vercel as a Flask app (entrypoint: app:app).

Replaces server.py for hosting. Same endpoints and responses as the local demo,
with three differences, because a Vercel function can't keep anything in memory
between requests:
  * All data lives in Neon (see db.py), not in attendance.sqlite3 and faces.json.
  * POST /api/camera checks the frame right away instead of handing it to a
    background loop. The result is returned and also published for /api/display.
  * Pairing keys come from the ADMIN_KEY and PHONE_KEY environment variables
    instead of being printed at startup.

Pages are static files in public/. Vercel serves them directly; when you run
the app locally with `flask --app app run`, this app serves them too.

Environment variables:
  DATABASE_URL   Neon connection string (use the pooled one on Vercel)
  ADMIN_KEY      key for the organizer's computer (required on Vercel)
  PHONE_KEY      key for the attendee phone (optional)
"""

import base64
import binascii
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, g, jsonify, request, send_from_directory

load_dotenv(Path(__file__).with_name(".env"))

import db  # noqa: E402  (load .env first)
import face_engine  # noqa: E402

ON_VERCEL = bool(os.environ.get("VERCEL"))
PUBLIC_DIR = Path(__file__).with_name("public")
MAX_JPEG = 250_000
MAX_JSON = 4096
MAX_ENROLL_JSON = 350_000
FRAME_INTERVAL_SECONDS = 0.8  # check at most about one camera frame per 0.8 s

PHONE_PATHS = {"/api/display", "/api/camera"}  # all a paired phone may use
JSON_POSTS = {
    "/api/checkins", "/api/recognition", "/api/members/add", "/api/members/consent",
    "/api/members/dues", "/api/faces/delete", "/api/enroll", "/api/settings", "/api/reset",
}

app = Flask(__name__, static_folder=None)  # Vercel serves public/ itself
app.config["MAX_CONTENT_LENGTH"] = MAX_ENROLL_JSON + 10_000


def error(status, message):
    response = jsonify(error=message)
    response.status_code = status
    return response


@app.after_request
def no_store(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# ---------- database connection, one per request ----------

def conn():
    if "conn" not in g:
        g.conn_cm = db.connect()
        g.conn = g.conn_cm.__enter__()
    return g.conn


@app.teardown_request
def close_conn(exc):
    cm = g.pop("conn_cm", None)
    g.pop("conn", None)
    if cm is not None:
        cm.__exit__(None, None, None)


# ---------- pairing and access control ----------

def keys():
    return os.environ.get("ADMIN_KEY", ""), os.environ.get("PHONE_KEY", "")


def role():
    """'admin', 'phone' or None for this request."""
    admin_key, phone_key = keys()
    if not admin_key:
        # No key configured: allowed only for local testing, like `python server.py` without --secure.
        return None if ON_VERCEL else "admin"
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ") or request.cookies.get("blade_key", "")
    if supplied and secrets.compare_digest(supplied, admin_key):
        return "admin"
    if supplied and phone_key and secrets.compare_digest(supplied, phone_key):
        return "phone"
    return None


@app.before_request
def check_access():
    path = request.path
    if not path.startswith("/api/") or path in ("/api/pair", "/api/health"):
        return None
    if request.method == "POST":
        origin = request.headers.get("Origin")
        host = request.headers.get("Host", "")
        if origin and origin not in ("http://" + host, "https://" + host):
            return error(403, "Cross-origin requests are not allowed")
    if ON_VERCEL and not keys()[0]:
        return error(503, "Server not configured: set ADMIN_KEY in the Vercel project settings")
    current = role()
    if current == "admin" or (current == "phone" and path in PHONE_PATHS):
        return None
    return error(403, "Pair this device first" if current is None else "Admin access required")


@app.post("/api/pair")
def pair():
    admin_key, phone_key = keys()
    payload = request.get_json(silent=True)
    key = payload.get("key") if isinstance(payload, dict) else None
    if not isinstance(key, str) or not 0 < len(key) <= 200:
        return error(400, "Invalid request")
    if admin_key and secrets.compare_digest(key, admin_key):
        target = "/?admin=1"
    elif phone_key and secrets.compare_digest(key, phone_key):
        target = "/guest.html"
    else:
        return error(403, "Invalid pairing key")
    response = jsonify(target=target)
    secure = request.is_secure or request.headers.get("X-Forwarded-Proto") == "https"
    response.set_cookie("blade_key", key, httponly=True, samesite="Strict", secure=secure, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------- read endpoints ----------

@app.get("/api/health")
def health():
    """Quick deployment check. Shows only whether things are configured, never secrets."""
    try:
        conn().execute("SELECT 1 FROM kiosk_settings WHERE id = 1").fetchone()
        database = "ok"
    except Exception as e:
        database = f"error: {type(e).__name__}"
    return jsonify(ok=database == "ok", database=database, adminKeySet=bool(keys()[0]),
                   faceModel=face_engine.status)


@app.get("/api/state")
def state():
    data = db.state(conn())
    data.update(latestMatch=db.current_display(conn(), private=True),
                vision=dict(status=face_engine.status, error=face_engine.error))
    return jsonify(data)


@app.get("/api/display")
def display():
    return jsonify(db.current_display(conn()))


@app.get("/api/camera/frame")
def camera_frame():
    # The local demo relayed phone frames to the admin page. A Vercel function can't
    # hold frames between requests, and frames are never stored, so there's no preview.
    return "", 204


# ---------- camera frames: recognize right away ----------

def recognize(jpeg, settings):
    """Run one frame through the face model and check the person in if they match."""
    try:
        embedding = face_engine.embed(jpeg, settings["requireLiveness"])
    except face_engine.LivenessFailed as e:
        return dict(status="liveness_failed", method="face", message=str(e))
    except ValueError as e:
        return dict(status="camera_error", method="face", message=str(e)[:200])
    except Exception as e:
        return dict(status="camera_error", method="face", message=str(e)[:200])
    if embedding is None:
        return dict(status="idle")

    match = db.best_match(conn(), embedding)
    if match is None or match[2] > settings["threshold"]:
        result = dict(status="unknown", method="face")
        if match is not None:
            result["distance"] = round(float(match[2]), 3)
        return result

    member_id, epoch, distance = match
    # Settings may have changed while the model was running: recheck before awarding attendance.
    latest = db.get_settings(conn())
    if (not latest["running"] or latest["eventId"] != settings["eventId"]
            or latest["requireLiveness"] != settings["requireLiveness"]):
        return dict(status="idle")
    if distance > latest["threshold"]:
        return dict(status="unknown", method="face", distance=round(float(distance), 3))
    return db.checkin(conn(), member_id, latest["eventId"], "face", distance, epoch)


@app.post("/api/camera")
def camera():
    if request.headers.get("Content-Type") != "image/jpeg":
        return error(400, "Expected JPEG")
    body = request.get_data(cache=False)
    if not 0 < len(body) <= MAX_JPEG:
        return error(413, "Frame too large")
    if not body.startswith(b"\xff\xd8") or not body.endswith(b"\xff\xd9"):
        return error(400, "Invalid JPEG")

    settings = db.get_settings(conn())
    if not settings["running"]:
        return jsonify(status="received", result=dict(status="idle"))
    if not db.claim_frame_slot(conn(), FRAME_INTERVAL_SECONDS):
        return jsonify(status="received", skipped=True)

    result = recognize(body, settings)
    if db.get_settings(conn())["running"]:  # don't overwrite "idle" if the kiosk was stopped meanwhile
        db.publish(conn(), result)
        if result["status"] not in ("checked_in", "already_checked_in", "dues_required",
                                     "role_required", "not_enrolled", "member_not_found"):
            db.record(conn(), result)  # check-in results were already recorded by db.checkin
    return jsonify(status="received", result=result)


# ---------- JSON endpoints (admin) ----------

def json_payload(limit):
    if not 0 < (request.content_length or 0) <= limit:
        raise db.ApiError("Invalid request size")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise db.ApiError("Invalid JSON")
    return payload


@app.post("/api/<path:action>")
def json_actions(action):
    path = "/api/" + action
    if path not in JSON_POSTS:
        return error(404, "Endpoint not found")
    c = conn()
    try:
        payload = json_payload(MAX_ENROLL_JSON if path == "/api/enroll" else MAX_JSON)

        if path == "/api/recognition":
            if payload not in ({"status": "unknown"}, {"status": "idle"}):
                raise db.ApiError("Expected unknown or idle status")
            db.publish(c, payload)
            return jsonify(payload)
        if path == "/api/members/add":
            result = db.add_member(c, payload.get("name"))
        elif path == "/api/members/consent":
            result = db.set_consent(c, payload.get("memberId"), payload.get("consent"))
        elif path == "/api/members/dues":
            result = db.set_paid(c, payload.get("memberId"), payload.get("paid"))
        elif path == "/api/faces/delete":
            result = db.delete_face_data(c, payload.get("memberId"))
            db.publish(c, dict(status="idle"))
        elif path == "/api/settings":
            result = db.update_settings(c, payload)
            if not result["running"]:
                db.publish(c, dict(status="idle"))
        elif path == "/api/reset":
            if payload != {"confirm": True}:
                raise db.ApiError("Reset confirmation required")
            result = db.reset_attendance(c)
        elif path == "/api/enroll":
            result = enroll(c, payload)
        else:  # /api/checkins: manual or QR check-in
            if set(payload) != {"memberId", "eventId"}:
                raise db.ApiError("Expected memberId and eventId")
            result = db.checkin(c, payload["memberId"], payload["eventId"])
            db.publish(c, result)
        return jsonify(result)
    except (db.ApiError, ValueError, TypeError) as e:
        return error(400, str(e))
    except Exception as e:
        return error(503, "Face service unavailable: " + str(e)[:200])


def enroll(c, payload):
    row = db.member_or_error(c, payload.get("memberId"))
    if not row["consent"]:
        raise db.ApiError("Consent is required before capturing")
    try:
        raw = base64.b64decode(payload.get("image", ""), validate=True)
    except (binascii.Error, TypeError):
        raise db.ApiError("Invalid image")
    if not 0 < len(raw) <= MAX_JPEG or not raw.startswith(b"\xff\xd8"):
        raise db.ApiError("Invalid image")
    embedding = face_engine.embed(raw)
    if embedding is None:
        raise db.ApiError("No face found. Face the camera and try again.")
    return db.add_embedding(c, row["id"], embedding, row["epoch"])


@app.errorhandler(413)
def too_large(_):
    return error(413, "Request too large")


# ---------- pages ----------

@app.get("/")
@app.get("/<path:page>")
def pages(page="index.html"):
    """On Vercel, files in public/ are served before requests reach this app,
    so this only runs locally, or on Vercel for paths with no matching file."""
    if page.startswith("api/"):
        return error(404, "Endpoint not found")
    if not ON_VERCEL and (PUBLIC_DIR / page).is_file():
        return send_from_directory(PUBLIC_DIR, page)
    if page == "index.html":
        return ("<!doctype html><title>Blade check-in</title>"
                "<p>The check-in API is running, but the pages haven't been added yet. "
                "Put them in the <code>public/</code> folder. "
                "Check the setup at <a href='/api/health'>/api/health</a>.</p>")
    abort(404)
