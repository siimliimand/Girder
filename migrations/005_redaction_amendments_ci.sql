-- 005: redaction_log, spec_amendments, ci_check_results

CREATE TABLE IF NOT EXISTS redaction_log (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id     TEXT REFERENCES attempts(id),
  source_field   TEXT NOT NULL,
  pattern_matched TEXT NOT NULL,
  ts             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spec_amendments (
  id               TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL REFERENCES runs(id),
  task_id          TEXT REFERENCES tasks(id),
  reason           TEXT NOT NULL,
  suggested_change TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'approved', 'rejected', 'aborted')),
  guidance         TEXT,
  new_spec_hash    TEXT,
  resolved_at      TEXT
);

CREATE TABLE IF NOT EXISTS ci_check_results (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id               TEXT NOT NULL REFERENCES runs(id),
  check_name           TEXT NOT NULL,
  status               TEXT NOT NULL,
  conclusion           TEXT,
  url                  TEXT,
  log_excerpt_redacted TEXT,
  created_at           TEXT NOT NULL
);
