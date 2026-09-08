-- 008: token_usage gains run context (Sprint 2; plan.md Phase 1).
-- Spec generation and plan estimation make model calls before any attempt
-- exists, so attempt_id becomes nullable and every usage row carries a
-- run_id. SQLite cannot drop a NOT NULL constraint in place, so rebuild.
CREATE TABLE token_usage_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id TEXT REFERENCES attempts(id),
  run_id TEXT REFERENCES runs(id),
  model_role TEXT NOT NULL,
  model_id TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL,
  completion_tokens INTEGER NOT NULL,
  cost_usd REAL NOT NULL,
  estimated_before_call REAL NOT NULL,
  created_at TEXT NOT NULL
);
INSERT INTO token_usage_new (id, attempt_id, run_id, model_role, model_id, prompt_tokens, completion_tokens, cost_usd, estimated_before_call, created_at)
SELECT id, attempt_id, NULL, model_role, model_id, prompt_tokens, completion_tokens, cost_usd, estimated_before_call, created_at FROM token_usage;
DROP TABLE token_usage;
ALTER TABLE token_usage_new RENAME TO token_usage;
