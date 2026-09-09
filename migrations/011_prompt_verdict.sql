-- Sprint 6 hardening: (a) ScopeGuard verdict per tool call (R2: ALLOW vs
-- ALLOW_LOGGED vs VIOLATION becomes auditable in persistence); (b) exact
-- per-turn prompt snapshots for the post-mortem explorer (plan.md Phase 5
-- task 5), stored redacted.
ALTER TABLE tool_calls ADD COLUMN verdict TEXT;
CREATE TABLE attempt_prompts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id TEXT NOT NULL REFERENCES attempts(id),
  run_id TEXT,
  turn INTEGER NOT NULL,
  role TEXT NOT NULL,
  content_redacted TEXT NOT NULL,
  ts TEXT NOT NULL
);
CREATE INDEX idx_prompts_attempt ON attempt_prompts(attempt_id, turn);
