-- 012: Sprint 6 (Phase 5) persisted diff streams (impl-plan §10 run-detail
-- "diff viewer", plan.md §2.1 "diff streams" telemetry).
--
-- One row per attempt: the redacted `git diff base..head` of the attempt's
-- task branch, captured at verify_passed. Like attempt_prompts.content_redacted
-- (migration 011), diff_redacted is stored exactly as given — the CALLER
-- redacts before insert; enforcement lives upstream, the column name documents
-- the expectation.

CREATE TABLE attempt_diffs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id    TEXT NOT NULL REFERENCES attempts(id),
  task_id       TEXT,
  run_id        TEXT NOT NULL,
  base_commit   TEXT NOT NULL,
  head_commit   TEXT NOT NULL,
  diff_redacted TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
CREATE INDEX idx_diffs_run ON attempt_diffs(run_id);
