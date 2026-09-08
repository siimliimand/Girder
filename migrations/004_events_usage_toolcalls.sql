-- 004: agent_events, token_usage, tool_calls

CREATE TABLE IF NOT EXISTS agent_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT NOT NULL,
  event_type   TEXT NOT NULL,
  run_id       TEXT REFERENCES runs(id),
  attempt_id   TEXT REFERENCES attempts(id),
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_attempt ON agent_events(attempt_id, id);
CREATE INDEX IF NOT EXISTS idx_events_run ON agent_events(run_id, id);

CREATE TABLE IF NOT EXISTS token_usage (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id            TEXT NOT NULL REFERENCES attempts(id),
  model_role            TEXT NOT NULL,
  model_id              TEXT NOT NULL,
  prompt_tokens         INTEGER NOT NULL,
  completion_tokens     INTEGER NOT NULL,
  cost_usd              REAL NOT NULL,
  estimated_before_call REAL NOT NULL,
  created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_calls (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id            TEXT NOT NULL REFERENCES attempts(id),
  ts                    TEXT NOT NULL,
  tool_name             TEXT NOT NULL,
  input_json            TEXT NOT NULL,
  output_blob_redacted  TEXT,
  duration_ms           INTEGER,
  scope_violation       INTEGER NOT NULL DEFAULT 0,
  held                  INTEGER NOT NULL DEFAULT 0
);
