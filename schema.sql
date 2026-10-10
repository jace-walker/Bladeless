-- Face check-in database schema (Neon Postgres + pgvector).
-- Safe to run more than once: nothing is created twice.
-- Run it with:  python face_check.py setup
-- or paste it into the Neon console's SQL Editor.

CREATE EXTENSION IF NOT EXISTS vector;

-- Club members who opted in. Enrolling counts as consent, recorded in consented_at.
-- Names must be unique, since the name is how you pick out a member.
CREATE TABLE IF NOT EXISTS members (
  id           SERIAL PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,
  consented_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Face embeddings: 512 numbers per enrollment shot. No photos are ever stored.
-- ON DELETE CASCADE: deleting a member deletes their face data too.
CREATE TABLE IF NOT EXISTS face_embeddings (
  id        SERIAL PRIMARY KEY,
  member_id INT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  embedding vector(512) NOT NULL
);

-- Club events (meetings, workshops, socials).
CREATE TABLE IF NOT EXISTS events (
  id      SERIAL PRIMARY KEY,
  name    TEXT NOT NULL,
  held_on DATE NOT NULL DEFAULT CURRENT_DATE
);

-- Who attended what. The primary key stops anyone being checked in twice.
CREATE TABLE IF NOT EXISTS checkins (
  member_id     INT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  event_id      INT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  method        TEXT NOT NULL DEFAULT 'face',   -- 'face', 'qr' or 'manual'
  checked_in_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (member_id, event_id)
);

-- Upgrades a database set up by the earlier version, which stored emails.
ALTER TABLE members DROP COLUMN IF EXISTS email;
CREATE UNIQUE INDEX IF NOT EXISTS members_name_key ON members (name);
