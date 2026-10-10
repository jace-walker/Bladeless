-- Face check-in database schema (Neon Postgres + pgvector).
-- Used by both the web app (app.py on Vercel) and the terminal tool (face_check.py).
-- Safe to run more than once: nothing is created twice, and older databases are upgraded.
-- Run it with:  python face_check.py setup
-- or paste it into the Neon console's SQL Editor.

CREATE EXTENSION IF NOT EXISTS vector;

-- Club members. consented_at is NULL until the member agrees to face check-in;
-- revoking consent sets it back to NULL. epoch goes up whenever consent changes,
-- so a capture that started before the change is rejected.
CREATE TABLE IF NOT EXISTS members (
  id           SERIAL PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,
  display_id   TEXT UNIQUE,                     -- member code shown on the page, e.g. KH-0001
  paid         BOOLEAN NOT NULL DEFAULT false,  -- dues paid
  base_events  INT NOT NULL DEFAULT 0,          -- events attended before this system existed
  consented_at TIMESTAMPTZ,
  epoch        INT NOT NULL DEFAULT 0
);

-- Face embeddings: 512 numbers per enrollment shot. No photos are ever stored.
-- ON DELETE CASCADE: deleting a member deletes their face data too.
CREATE TABLE IF NOT EXISTS face_embeddings (
  id        SERIAL PRIMARY KEY,
  member_id INT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  embedding vector(512) NOT NULL
);

-- Club events. slug is an optional short id like 'intro-ai'.
-- audience: 'public' (anyone), 'dues' (paid members only) or 'roles' (not supported yet).
CREATE TABLE IF NOT EXISTS events (
  id         SERIAL PRIMARY KEY,
  name       TEXT NOT NULL,
  held_on    DATE NOT NULL DEFAULT CURRENT_DATE,
  slug       TEXT UNIQUE,
  date_label TEXT,                              -- e.g. 'Oct 13, 2026, 5:00 PM'
  location   TEXT,
  points     INT NOT NULL DEFAULT 10,
  audience   TEXT NOT NULL DEFAULT 'public' CHECK (audience IN ('public', 'dues', 'roles'))
);

-- Who attended what. The primary key stops anyone being checked in twice.
CREATE TABLE IF NOT EXISTS checkins (
  member_id     INT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  event_id      INT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  method        TEXT NOT NULL DEFAULT 'face',   -- 'face', 'qr' or 'manual'
  checked_in_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  distance      REAL,                           -- face match distance, for tuning
  PRIMARY KEY (member_id, event_id)
);

-- Kiosk settings: a single row the admin page edits.
CREATE TABLE IF NOT EXISTS kiosk_settings (
  id               INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  event_id         INT REFERENCES events(id) ON DELETE SET NULL,
  threshold        REAL NOT NULL DEFAULT 0.30 CHECK (threshold BETWEEN 0.10 AND 0.60),
  require_liveness BOOLEAN NOT NULL DEFAULT false,
  running          BOOLEAN NOT NULL DEFAULT false
);
INSERT INTO kiosk_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- The latest kiosk result (what the attendee screen shows), plus when the last
-- camera frame was processed, so frames arriving too quickly can be skipped.
CREATE TABLE IF NOT EXISTS kiosk_status (
  id            INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  result        JSONB NOT NULL DEFAULT '{"status": "idle"}',
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_frame_at TIMESTAMPTZ NOT NULL DEFAULT 'epoch'
);
INSERT INTO kiosk_status (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- Recent check-in results for the admin page (only the newest 100 are kept).
CREATE TABLE IF NOT EXISTS checkin_log (
  id         BIGSERIAL PRIMARY KEY,
  result     JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- Upgrades for databases created by earlier versions ----------

-- The first version stored emails.
ALTER TABLE members DROP COLUMN IF EXISTS email;
CREATE UNIQUE INDEX IF NOT EXISTS members_name_key ON members (name);

-- The terminal-only version had no dues, codes or consent tracking. Members it
-- enrolled agreed to consent at the time, so their consented_at stays as it is.
ALTER TABLE members ADD COLUMN IF NOT EXISTS display_id TEXT UNIQUE;
ALTER TABLE members ADD COLUMN IF NOT EXISTS paid BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE members ADD COLUMN IF NOT EXISTS base_events INT NOT NULL DEFAULT 0;
ALTER TABLE members ADD COLUMN IF NOT EXISTS epoch INT NOT NULL DEFAULT 0;
ALTER TABLE members ALTER COLUMN consented_at DROP NOT NULL;
ALTER TABLE members ALTER COLUMN consented_at DROP DEFAULT;

ALTER TABLE events ADD COLUMN IF NOT EXISTS slug TEXT UNIQUE;
ALTER TABLE events ADD COLUMN IF NOT EXISTS date_label TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS location TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS points INT NOT NULL DEFAULT 10;
ALTER TABLE events ADD COLUMN IF NOT EXISTS audience TEXT NOT NULL DEFAULT 'public'
  CHECK (audience IN ('public', 'dues', 'roles'));

ALTER TABLE checkins ADD COLUMN IF NOT EXISTS distance REAL;
