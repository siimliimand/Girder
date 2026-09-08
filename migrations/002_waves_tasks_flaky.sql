-- 002: waves, tasks, flaky_tests

CREATE TABLE IF NOT EXISTS waves (
  id             TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL REFERENCES runs(id),
  sequence_order INTEGER NOT NULL,
  status         TEXT NOT NULL DEFAULT 'pending',
  UNIQUE (run_id, sequence_order)
);

CREATE TABLE IF NOT EXISTS tasks (
  id               TEXT PRIMARY KEY,
  wave_id          TEXT NOT NULL REFERENCES waves(id),
  seq              INTEGER NOT NULL,
  title            TEXT NOT NULL,
  task_type        TEXT NOT NULL CHECK (task_type IN
    ('code_change', 'test_change', 'fix', 'refactor', 'documentation')),
  scope_globs_json TEXT NOT NULL DEFAULT '[]',
  spec_slice_md    TEXT NOT NULL,
  status           TEXT NOT NULL CHECK (status IN (
    'pending', 'scheduled', 'running', 'verifying', 'verify_passed', 'retry_scheduled',
    'awaiting_amendment', 'completed', 'failed', 'skipped', 'force_passed', 'dropped')),
  test_content_hash TEXT,
  attempts_used    INTEGER NOT NULL DEFAULT 0,
  depends_on_json  TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_tasks_wave ON tasks(wave_id, seq);

CREATE TABLE IF NOT EXISTS flaky_tests (
  project_id     TEXT NOT NULL,
  test_id        TEXT NOT NULL,
  first_seen_run TEXT,
  last_seen_run  TEXT,
  status         TEXT NOT NULL DEFAULT 'known_flaky',
  PRIMARY KEY (project_id, test_id)
);
