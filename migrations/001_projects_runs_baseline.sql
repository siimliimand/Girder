-- 001: projects, runs, baseline_runs (impl-plan §4.3)
-- All DDL is idempotent (IF NOT EXISTS): a crash between script application and
-- version recording must be safe to re-apply on the next boot.

CREATE TABLE IF NOT EXISTS projects (
  id            TEXT PRIMARY KEY,
  name          TEXT UNIQUE NOT NULL,
  repo_path     TEXT NOT NULL,
  autonomy_tier INTEGER NOT NULL DEFAULT 0 CHECK (autonomy_tier IN (0, 1, 2)),
  clean_merge_streak INTEGER NOT NULL DEFAULT 0,
  config_json   TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id),
  intent        TEXT NOT NULL,
  branch        TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN (
    'draft', 'spec_pending', 'spec_approved', 'baseline_running', 'active',
    'awaiting_amendment', 'pr_open', 'ci_running', 'ci_fixing', 'conformance_review',
    'merge_pending_human', 'merged', 'failed', 'aborted', 'budget_exhausted', 'escalated')),
  spec_hash     TEXT,
  budget_cap_usd      REAL NOT NULL,
  spend_usd           REAL NOT NULL DEFAULT 0,
  projected_spend_usd REAL NOT NULL DEFAULT 0,
  baseline_run_id     TEXT REFERENCES baseline_runs(id),
  pr_number     INTEGER,
  integrity_violations INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id, status);

CREATE TABLE IF NOT EXISTS baseline_runs (
  id           TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES projects(id),
  commit_sha   TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  per_test_json TEXT NOT NULL DEFAULT '{}'
);
