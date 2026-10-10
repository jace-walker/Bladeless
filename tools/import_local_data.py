"""Copy data from the old local demo (faces.json + attendance.sqlite3) into Neon.

Run this once on the computer that has those files, after `python face_check.py setup`
and `python face_check.py seed`:

    python tools/import_local_data.py --faces faces.json --sqlite attendance.sqlite3

* Members from attendance.sqlite3 are added to Neon (matched by name), with their
  member code, dues status and consent.
* Face embeddings from faces.json are added for the matching member. Members who
  already have face data in Neon are skipped, so running this twice is safe.
* Attendance is copied for events that exist in Neon with the same id (e.g. 'intro-ai').

Only embeddings are copied. No photos exist in either file.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))
import db  # noqa: E402


def load_demo_members(path):
    """face_name -> member info, from the demo's SQLite database."""
    if not path or not Path(path).exists():
        return {}, ({}, [])
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM demo_members").fetchall()
    except sqlite3.OperationalError:
        rows = []
    members = {r["face_name"]: dict(r) for r in rows}
    by_demo_id = {r["id"]: dict(r) for r in rows}
    try:
        attendance = [dict(r) for r in con.execute("SELECT member_id, event_id FROM attendance")]
    except sqlite3.OperationalError:
        attendance = []
    con.close()
    return members, (by_demo_id, attendance)


def upsert_member(conn, name, info):
    """Add the member, or update them from the demo database. Consent is only ever
    added, never removed. Without demo info (faces.json only), the member starts
    without consent, as in the local demo; opt them in on the profile page."""
    if not info:
        return conn.execute(
            "INSERT INTO members (name) VALUES (%s) ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
            (name,),
        ).fetchone()[0]
    return conn.execute(
        """INSERT INTO members (name, display_id, paid, base_events, consented_at)
           VALUES (%s, %s, %s, %s, CASE WHEN %s THEN now() END)
           ON CONFLICT (name) DO UPDATE
           SET display_id = COALESCE(members.display_id, EXCLUDED.display_id),
               paid = EXCLUDED.paid,
               base_events = EXCLUDED.base_events,
               consented_at = COALESCE(members.consented_at, EXCLUDED.consented_at)
           RETURNING id""",
        (name, info.get("display_id"), bool(info.get("paid")), int(info.get("base_events") or 0),
         bool(info.get("consent"))),
    ).fetchone()[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--faces", default="faces.json")
    parser.add_argument("--sqlite", default="attendance.sqlite3")
    args = parser.parse_args()

    faces = json.loads(Path(args.faces).read_text()) if Path(args.faces).exists() else {}
    members, (by_demo_id, attendance) = load_demo_members(args.sqlite)
    if not faces and not members:
        sys.exit("Found neither faces.json nor attendance.sqlite3. Pass their paths with --faces and --sqlite.")

    with db.connect() as conn:
        # Members from the demo database, plus anyone who only appears in faces.json.
        ids = {}
        for face_name in set(members) | set(faces):
            info = members.get(face_name)
            name = info["name"] if info else face_name
            ids[face_name] = upsert_member(conn, name, info)
            print(f"Member: {name}")

        for face_name, vectors in faces.items():
            member_id = ids[face_name]
            have = conn.execute(
                "SELECT count(*) FROM face_embeddings WHERE member_id = %s", (member_id,)
            ).fetchone()[0]
            if have:
                print(f"  {face_name}: already has {have} embeddings in Neon, skipped")
                continue
            good = [v for v in vectors if len(v) == 512 and np.all(np.isfinite(v))]
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO face_embeddings (member_id, embedding) VALUES (%s, %s)",
                    [(member_id, np.asarray(v, dtype=np.float32)) for v in good],
                )
            print(f"  {face_name}: copied {len(good)} embeddings")

        copied = 0
        for row in attendance:
            demo_member = by_demo_id.get(row["member_id"])
            event = db.find_event(conn, row["event_id"])
            if not demo_member or not event or demo_member["face_name"] not in ids:
                continue
            copied += conn.execute(
                """INSERT INTO checkins (member_id, event_id, method) VALUES (%s, %s, 'imported')
                   ON CONFLICT (member_id, event_id) DO NOTHING""",
                (ids[demo_member["face_name"]], event["id"]),
            ).rowcount
        print(f"Attendance: copied {copied} check-ins")
    print("Done. You can delete faces.json and attendance.sqlite3 once the web app works.")


if __name__ == "__main__":
    main()
