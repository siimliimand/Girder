-- 006: integrity_violations, notifications_log

CREATE TABLE IF NOT EXISTS integrity_violations (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id       TEXT NOT NULL,
  task_id      TEXT,
  attempt_id   TEXT,
  kind         TEXT NOT NULL CHECK (kind IN (
    'test_path_modified', 'content_hash_mismatch', 'out_of_scope_write',
    'protected_read', 'scope_violation')),
  detail_json  TEXT NOT NULL,
  ts           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications_log (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  channel          TEXT NOT NULL,
  payload_redacted TEXT NOT NULL,
  status           TEXT NOT NULL,
  ts               TEXT NOT NULL
);
