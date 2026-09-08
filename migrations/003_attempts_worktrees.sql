-- 003: attempts, worktrees

CREATE TABLE IF NOT EXISTS attempts (
  id             TEXT PRIMARY KEY,
  task_id        TEXT NOT NULL REFERENCES tasks(id),
  attempt_num    INTEGER NOT NULL,
  base_commit    TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN (
    'initialized', 'running', 'succeeded', 'failed', 'timeout', 'crashed',
    'budget_frozen', 'amendment_requested', 'integrity_violation')),
  exit_code      INTEGER,
  turns_used     INTEGER NOT NULL DEFAULT 0,
  worktree_path  TEXT,
  container_id   TEXT,
  failure_reason TEXT,
  started_at     TEXT,
  ended_at       TEXT,
  UNIQUE (task_id, attempt_num)
);

CREATE TABLE IF NOT EXISTS worktrees (
  id         TEXT PRIMARY KEY,
  attempt_id TEXT NOT NULL REFERENCES attempts(id),
  path       TEXT NOT NULL UNIQUE,
  branch     TEXT NOT NULL,
  state      TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'pruned', 'quarantined')),
  created_at TEXT NOT NULL,
  removed_at TEXT
);
