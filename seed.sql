-- Demo events and made-up members for trying the app.
-- Safe to run more than once. Existing members keep their face data and settings;
-- only a missing member code is filled in.
-- Run it with:  python face_check.py seed

INSERT INTO events (slug, name, held_on, date_label, location, points, audience) VALUES
  ('intro-ai', 'Intro to Artificial Intelligence', '2026-10-13', 'Oct 13, 2026, 5:00 PM', 'HEC 101', 10, 'public'),
  ('intro-github', 'Intro to GitHub and GitHub Etiquette (SASE X KH)', '2026-10-20', 'Oct 20, 2026, 5:00 PM', 'HEC 101', 10, 'dues')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO members (name, display_id, paid, base_events) VALUES
  ('Alex Rivera', 'KH-0003', true, 6),
  ('Sam Chen', 'KH-0001', true, 3),
  ('Jordan Lee', 'KH-0002', false, 1),
  ('Taylor Kim', 'KH-6659', false, 7)
ON CONFLICT (name) DO UPDATE
  SET display_id = COALESCE(members.display_id, EXCLUDED.display_id);

-- Point the kiosk at the first demo event if it isn't pointing anywhere yet.
UPDATE kiosk_settings
   SET event_id = (SELECT id FROM events WHERE slug = 'intro-ai')
 WHERE id = 1 AND event_id IS NULL;
