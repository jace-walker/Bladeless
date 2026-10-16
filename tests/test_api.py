"""API tests for app.py against a real Postgres database with pgvector.

These tests ERASE every table in the database they use, so they only run when
TEST_DATABASE_URL points at a separate test database (never your real Neon one).
A Neon branch made just for testing works well. The face model is replaced with
a stand-in, so the tests take seconds and need no camera.

    TEST_DATABASE_URL=postgresql://... python -m pytest tests -q
"""

import importlib
import os
import sys
from pathlib import Path

import numpy as np
import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="set TEST_DATABASE_URL to a throwaway database")

ADMIN, PHONE = "admin-test-key", "phone-test-key"
TABLES = "checkin_log, kiosk_status, kiosk_settings, checkins, face_embeddings, events, members"


def jpeg(label):
    """A fake 'photo'. The stand-in face model reads the label back out."""
    return b"\xff\xd8" + label.encode() + b"\xff\xd9"


def fake_embed(data, anti_spoofing=False):
    import face_engine
    label = data[2:-2].decode()
    if label == "noface":
        return None
    if label == "spoof" and anti_spoofing:
        raise face_engine.LivenessFailed("Liveness check failed")
    person, _, shot = label.partition("#")
    base = np.random.default_rng(sum(map(ord, person))).normal(size=512)
    noise = np.random.default_rng(len(label) * 7 + sum(map(ord, shot))).normal(size=512) * 0.05
    return [float(v) for v in base + noise]


def fake_embed_faces(data, anti_spoofing=False):
    """Several faces in one frame: 'Sam Chen#1+Stranger' is two people."""
    faces = []
    for i, label in enumerate(data[2:-2].decode().split("+")):
        embedding = fake_embed(b"\xff\xd8" + label.encode() + b"\xff\xd9", anti_spoofing)
        if embedding is not None:
            faces.append(dict(embedding=embedding, box=dict(x=10 + 150 * i, y=40, w=120, h=150)))
    return faces


@pytest.fixture()
def app_module(monkeypatch):
    if TEST_URL == os.environ.get("DATABASE_URL"):
        pytest.exit("TEST_DATABASE_URL must not be your real DATABASE_URL")
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setenv("ADMIN_KEY", ADMIN)
    monkeypatch.setenv("PHONE_KEY", PHONE)
    monkeypatch.setenv("PHONE_CHECKIN", "on")
    monkeypatch.delenv("VERCEL", raising=False)
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {TABLES} CASCADE")
        conn.execute((ROOT / "schema.sql").read_text())
        conn.execute((ROOT / "seed.sql").read_text())
    import app as app_module
    app_module = importlib.reload(app_module)
    monkeypatch.setattr(app_module.face_engine, "embed", fake_embed)
    monkeypatch.setattr(app_module.face_engine, "embed_faces", fake_embed_faces)
    monkeypatch.setattr(app_module.face_engine, "warm", lambda: None)
    monkeypatch.delenv("CRON_SECRET", raising=False)
    monkeypatch.setattr(app_module, "FRAME_INTERVAL_SECONDS", 0)
    return app_module


@pytest.fixture()
def admin(app_module):
    client = app_module.app.test_client()
    assert client.post("/api/pair", json={"key": ADMIN}).status_code == 200
    return client


@pytest.fixture()
def phone(app_module):
    client = app_module.app.test_client()
    assert client.post("/api/pair", json={"key": PHONE}).get_json()["target"] == "/guest.html"
    return client


def member(client, name):
    return next(m for m in client.get("/api/state").get_json()["members"] if m["name"] == name)


def enroll(client, name, shots=5):
    m = member(client, name)
    assert client.post("/api/members/consent", json={"memberId": m["id"], "consent": True}).status_code == 200
    import base64
    for i in range(shots):
        r = client.post("/api/enroll", json={"memberId": m["id"], "image": base64.b64encode(jpeg(f"{name}#{i}")).decode()})
        assert r.status_code == 200, r.get_json()
    return member(client, name)


def frame(client, label):
    r = client.post("/api/camera", data=jpeg(label), content_type="image/jpeg")
    assert r.status_code == 200, r.get_json()
    return r.get_json()["result"]


def start(client, event="intro-ai", **extra):
    r = client.post("/api/settings", json={"eventId": event, "running": True, **extra})
    assert r.status_code == 200, r.get_json()


def test_health_needs_no_key(app_module):
    data = app_module.app.test_client().get("/api/health").get_json()
    assert data["ok"] and data["adminKeySet"] and not data["openAccess"] and data["phoneCheckin"]
    assert data["nightlyReset"] is False


def test_pairing_and_access(app_module, phone):
    anon = app_module.app.test_client()
    assert anon.get("/api/state").status_code == 403
    assert anon.post("/api/pair", json={"key": "wrong"}).status_code == 403
    assert phone.get("/api/display").get_json() == {"status": "idle"}
    assert phone.get("/api/state").get_json()["error"] == "Admin access required"
    bearer = app_module.app.test_client()
    assert bearer.get("/api/state", headers={"Authorization": "Bearer " + ADMIN}).status_code == 200


def test_cross_origin_post_rejected(admin):
    r = admin.post("/api/members/add", json={"name": "X"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_seeded_state(admin):
    data = admin.get("/api/state").get_json()
    assert {e["id"] for e in data["events"]} == {"intro-ai", "intro-github"}
    assert {m["name"] for m in data["members"]} == {"Alex Rivera", "Sam Chen", "Jordan Lee", "Taylor Kim"}
    assert data["settings"] == {"eventId": "intro-ai", "threshold": 0.3, "requireLiveness": False, "running": False}
    assert member(admin, "Taylor Kim")["eventsAttended"] == 7


def test_members_consent_and_enrollment(admin):
    new = admin.post("/api/members/add", json={"name": "Dana"}).get_json()
    assert new["displayId"].startswith("KH-") and not new["consent"]
    assert admin.post("/api/members/add", json={"name": "dana"}).get_json()["error"] == "That name already exists"

    import base64
    image = base64.b64encode(jpeg("Dana#0")).decode()
    r = admin.post("/api/enroll", json={"memberId": new["id"], "image": image})
    assert r.get_json()["error"] == "Consent is required before capturing"

    dana = enroll(admin, "Dana")
    assert dana["samples"] == 5 and dana["enrolled"]
    r = admin.post("/api/enroll", json={"memberId": dana["id"], "image": base64.b64encode(jpeg("noface")).decode()})
    assert r.get_json()["error"] == "No face found. Face the camera and try again."


def test_face_checkin_flow(admin, phone):
    enroll(admin, "Sam Chen")
    assert frame(admin, "Sam Chen#x")["status"] == "idle"  # kiosk not running yet

    start(admin)
    result = frame(phone, "Sam Chen#live")
    assert result["status"] == "checked_in" and result["pointsAwarded"] == 10
    assert result["member"]["name"] == "Sam Chen"
    assert phone.get("/api/display").get_json() == {"status": "checked_in"}  # no name for attendees
    assert admin.get("/api/state").get_json()["latestMatch"]["member"]["name"] == "Sam Chen"

    assert frame(phone, "Sam Chen#again")["status"] == "already_checked_in"
    assert frame(phone, "Stranger")["status"] == "unknown"
    assert frame(phone, "noface")["status"] == "idle"

    state = admin.get("/api/state").get_json()
    assert len(state["attendance"]) == 1 and state["attendance"][0]["event_id"] == "intro-ai"
    assert member(admin, "Sam Chen")["eventsAttended"] == 4
    assert {r["status"] for r in state["recent"]} >= {"checked_in", "already_checked_in", "unknown"}


def test_dues_only_event(admin):
    enroll(admin, "Jordan Lee")
    start(admin, "intro-github")
    assert frame(admin, "Jordan Lee#1")["status"] == "dues_required"
    jordan = member(admin, "Jordan Lee")
    admin.post("/api/members/dues", json={"memberId": jordan["id"], "paid": True})
    assert frame(admin, "Jordan Lee#2")["status"] == "checked_in"


def test_liveness(admin):
    start(admin, requireLiveness=True)
    assert frame(admin, "spoof")["status"] == "liveness_failed"


def test_manual_checkin_by_code_and_qr(admin):
    r = admin.post("/api/checkins", json={"memberId": "KH-0002", "eventId": "intro-ai"}).get_json()
    assert r["status"] == "checked_in" and r["method"] == "manual"
    taylor = member(admin, "Taylor Kim")
    assert admin.post("/api/checkins", json={"memberId": f"member:{taylor['id']}", "eventId": "intro-ai"}).get_json()["status"] == "checked_in"
    assert admin.post("/api/checkins", json={"memberId": "KH-NOPE", "eventId": "intro-ai"}).get_json()["status"] == "member_not_found"
    assert admin.post("/api/checkins", json={"memberId": "KH-0002", "eventId": "nope"}).status_code == 400


def test_revoked_consent_and_deletion(admin):
    sam = enroll(admin, "Sam Chen")
    start(admin)
    admin.post("/api/members/consent", json={"memberId": sam["id"], "consent": False})
    assert frame(admin, "Sam Chen#1")["status"] == "unknown"  # not a candidate without consent
    admin.post("/api/faces/delete", json={"memberId": sam["id"]})
    sam = member(admin, "Sam Chen")
    assert sam["samples"] == 0 and not sam["consent"]


def test_settings_validation(admin):
    assert admin.post("/api/settings", json={"threshold": 0.9}).status_code == 400
    assert admin.post("/api/settings", json={"eventId": "nope"}).status_code == 400
    assert admin.post("/api/settings", json={"bogus": 1}).status_code == 400
    assert admin.post("/api/settings", json={"threshold": 0.25}).get_json()["threshold"] == 0.25


def test_reset_keeps_members(admin):
    enroll(admin, "Sam Chen")
    start(admin)
    frame(admin, "Sam Chen#1")
    assert admin.post("/api/reset", json={"confirm": False}).status_code == 400
    assert admin.post("/api/reset", json={"confirm": True}).get_json() == {"status": "reset"}
    state = admin.get("/api/state").get_json()
    assert state["attendance"] == [] and state["recent"] == [] and not state["settings"]["running"]
    assert member(admin, "Sam Chen")["samples"] == 5 and member(admin, "Sam Chen")["eventsAttended"] == 3


def test_frames_are_throttled(app_module, admin, monkeypatch):
    enroll(admin, "Sam Chen")
    start(admin)
    monkeypatch.setattr(app_module, "FRAME_INTERVAL_SECONDS", 5)
    first = admin.post("/api/camera", data=jpeg("Stranger"), content_type="image/jpeg").get_json()
    second = admin.post("/api/camera", data=jpeg("Stranger"), content_type="image/jpeg").get_json()
    assert "result" in first and second.get("skipped") is True


def test_bad_frames(admin):
    assert admin.post("/api/camera", data=b"not a jpeg", content_type="image/jpeg").status_code == 400
    assert admin.post("/api/camera", data=jpeg("x"), content_type="text/plain").status_code == 400


def test_open_access_without_admin_key(app_module, monkeypatch):
    # No ADMIN_KEY, even on Vercel: an open demo where nobody needs to pair
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("ADMIN_KEY")
    module = importlib.reload(app_module)
    monkeypatch.setattr(module.face_engine, "embed", fake_embed)
    anyone = module.app.test_client()
    assert anyone.get("/api/health").get_json()["openAccess"] is True
    state = anyone.get("/api/state")
    assert state.status_code == 200 and state.get_json()["access"]["open"] is True
    assert anyone.post("/api/members/add", json={"name": "Visitor"}).status_code == 200
    assert anyone.post("/api/pair", json={}).get_json() == {"target": "/checkin.html"}  # nothing to pair
    # Cross-site requests are still refused
    r = anyone.post("/api/members/add", json={"name": "X"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_phone_checkin_off(app_module, monkeypatch, phone):
    monkeypatch.delenv("PHONE_CHECKIN")  # the default: off
    client = app_module.app.test_client()
    assert client.get("/api/health").get_json()["phoneCheckin"] is False
    r = client.post("/api/pair", json={"key": PHONE})
    assert r.status_code == 403 and r.get_json()["error"] == "Phone check-in is turned off"
    assert phone.get("/api/display").status_code == 403  # a phone paired earlier is shut out too
    admin = app_module.app.test_client()
    admin.post("/api/pair", json={"key": ADMIN})
    access = admin.get("/api/state").get_json()["access"]
    assert access["open"] is False and access["phoneCheckin"] is False


def test_remove_member(app_module, admin):
    sam = enroll(admin, "Sam Chen")
    start(admin)
    assert frame(admin, "Sam Chen#1")["status"] == "checked_in"
    r = admin.post("/api/members/remove", json={"memberId": sam["id"]}).get_json()
    assert r == {"status": "removed", "name": "Sam Chen"}
    state = admin.get("/api/state").get_json()
    assert "Sam Chen" not in {m["name"] for m in state["members"]}
    assert state["attendance"] == []  # their check-ins went with them
    with psycopg.connect(TEST_URL) as conn:  # and so did their face data
        assert conn.execute("SELECT count(*) FROM face_embeddings").fetchone()[0] == 0
    assert frame(admin, "Sam Chen#2")["status"] == "unknown"
    assert admin.post("/api/members/remove", json={"memberId": sam["id"]}).get_json()["error"] == "Member not found"


def test_create_event(admin):
    r = admin.post("/api/events/add", json={
        "name": "Hack Night!", "date": "2026-10-27", "time": "18:30",
        "location": "HEC 101", "points": 15, "duesOnly": True,
    })
    event = r.get_json()
    assert r.status_code == 200, event
    assert event["id"] == "hack-night" and event["date"] == "Oct 27, 2026, 6:30 PM"
    assert event["audience"] == "dues" and event["points"] == 15 and event["checkins"] == 0
    # Same name again gets its own id; no time shows just the date
    second = admin.post("/api/events/add", json={"name": "Hack Night!", "date": "2026-11-03"}).get_json()
    assert second["id"] == "hack-night-2" and second["date"] == "Nov 3, 2026" and second["audience"] == "public"
    # Purely numeric names can't be mistaken for a database id
    assert admin.post("/api/events/add", json={"name": "2026", "date": "2026-11-04"}).get_json()["id"] == "event-2026"
    events = admin.get("/api/state").get_json()["events"]
    assert {"hack-night", "hack-night-2", "event-2026"} <= {e["id"] for e in events}
    start(admin, "hack-night")  # the kiosk can use it right away


def test_event_validation(admin):
    def error(body):
        r = admin.post("/api/events/add", json=body)
        assert r.status_code == 400
        return r.get_json()["error"]
    assert error({"date": "2026-10-27"}) == "Enter an event name of 1–100 characters"
    assert error({"name": "X"}) == "Choose a date for the event"
    assert error({"name": "X", "date": "2026-02-30"}) == "Choose a date for the event"
    assert "start time" in error({"name": "X", "date": "2026-10-27", "time": "6pm"})
    assert "Points" in error({"name": "X", "date": "2026-10-27", "points": True})
    assert "Points" in error({"name": "X", "date": "2026-10-27", "points": 5000})
    assert error({"name": "X", "date": "2026-10-27", "bogus": 1}) == "Unknown event field"


def test_delete_event_rules(admin):
    new = admin.post("/api/events/add", json={"name": "Social", "date": "2026-11-10"}).get_json()
    # The running kiosk's event can't be deleted
    start(admin, new["id"])
    assert "Stop the kiosk" in admin.post("/api/events/delete", json={"eventId": new["id"]}).get_json()["error"]
    admin.post("/api/settings", json={"running": False})
    # An event with check-ins can't be deleted
    admin.post("/api/checkins", json={"memberId": "KH-0002", "eventId": new["id"]})
    assert "checked in" in admin.post("/api/events/delete", json={"eventId": new["id"]}).get_json()["error"]
    # An event nobody attended can
    empty = admin.post("/api/events/add", json={"name": "Cancelled", "date": "2026-11-11"}).get_json()
    assert admin.post("/api/events/delete", json={"eventId": empty["id"]}).get_json() == {"status": "deleted"}
    assert empty["id"] not in {e["id"] for e in admin.get("/api/state").get_json()["events"]}
    assert admin.post("/api/events/delete", json={"eventId": "nope"}).get_json()["error"] == "Event not found"


def test_multiple_faces(admin):
    enroll(admin, "Sam Chen")
    enroll(admin, "Alex Rivera")
    start(admin)
    result = frame(admin, "Sam Chen#a+Alex Rivera#b+Stranger")
    faces = result["faces"]
    assert [f["status"] for f in faces] == ["checked_in", "checked_in", "unknown"]
    assert [f.get("member", {}).get("name") for f in faces] == ["Sam Chen", "Alex Rivera", None]
    assert all(set(f["box"]) == {"x", "y", "w", "h"} for f in faces)
    assert result["status"] == "checked_in" and result["member"]["name"] == "Sam Chen"  # nearest face
    state = admin.get("/api/state").get_json()
    assert len(state["attendance"]) == 2
    assert {r["status"] for r in state["recent"]} == {"checked_in", "unknown"}
    assert all("box" not in r for r in state["recent"])
    # Next frame: both are already in; one face alone still works the old way
    again = frame(admin, "Sam Chen#c+Alex Rivera#d")
    assert [f["status"] for f in again["faces"]] == ["already_checked_in", "already_checked_in"]
    assert len(frame(admin, "Sam Chen#e")["faces"]) == 1


def test_warmup(app_module, admin, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module.face_engine, "warm", lambda: calls.append(1))
    r = admin.post("/api/warmup", json={})
    assert r.status_code == 200 and calls == [1]

    def broken():
        raise RuntimeError("weights missing")
    monkeypatch.setattr(app_module.face_engine, "warm", broken)
    r = admin.post("/api/warmup", json={})
    assert r.status_code == 503 and "weights missing" in r.get_json()["error"]


def test_nightly_reset(app_module, admin, monkeypatch):
    anyone = app_module.app.test_client()
    url = "/api/cron/reset-demo"
    assert anyone.get(url).status_code == 503  # off until CRON_SECRET is set
    monkeypatch.setenv("CRON_SECRET", "cron-test-secret")
    assert admin.get("/api/health").get_json()["nightlyReset"] is True
    assert anyone.get(url).status_code == 403
    assert anyone.get(url, headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert anyone.get(url, headers={"Authorization": "Bearer caf\u00e9"}).status_code == 403

    # Visitors add a member, enroll, check in, create an event and change settings
    admin.post("/api/members/add", json={"name": "Visitor"})
    enroll(admin, "Visitor")
    enroll(admin, "Sam Chen")
    start(admin, threshold=0.4)
    assert frame(admin, "Sam Chen#1")["status"] == "checked_in"
    admin.post("/api/events/add", json={"name": "Visitor Party", "date": "2026-11-01"})

    r = anyone.get(url, headers={"Authorization": "Bearer cron-test-secret"})
    assert r.status_code == 200 and r.get_json() == {"status": "reset", "faceShotsDeleted": 10}
    state = admin.get("/api/state").get_json()
    assert {m["name"] for m in state["members"]} == {"Alex Rivera", "Sam Chen", "Jordan Lee", "Taylor Kim"}
    assert all(m["samples"] == 0 and not m["consent"] for m in state["members"])
    assert {e["id"] for e in state["events"]} == {"intro-ai", "intro-github"}
    assert state["attendance"] == [] and state["recent"] == []
    assert state["settings"] == {"eventId": "intro-ai", "threshold": 0.3, "requireLiveness": False, "running": False}
    assert member(admin, "Taylor Kim")["eventsAttended"] == 7
