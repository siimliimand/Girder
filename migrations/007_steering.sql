-- 007: steering_events (Phase 5 control surface; table lands now so the
-- recovery service can drain stale steering events from day one).

CREATE TABLE IF NOT EXISTS steering_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id       TEXT NOT NULL,
  kind         TEXT NOT NULL CHECK (kind IN
    ('pause', 'resume', 'abort', 'inject', 'skip', 'force_pass')),
  payload_json TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  consumed_at  TEXT
);
