-- 015: WS-04 intent clarification loop (plan §8.5 / docs/improvements/
-- ws04-clarify-loop.md). Two additive schema changes:
--
-- 1. Widen the runs.status CHECK with the new 'clarifying' literal. The runs
--    table is referenced by seven child tables, so a DROP/rebuild is not
--    possible while FK enforcement is on (DROP TABLE runs an implicit DELETE
--    whose parent-side FK violations are checked immediately, even for
--    deferred constraints). Instead the CHECK literal list is widened in
--    place via a bounded writable_schema edit: the replace() anchor is the
--    exact substring "'draft', 'spec_pending'", which occurs in exactly one
--    schema row (runs, from migration 001) — verified at authoring time. No
--    data rows are touched and the table is re-read (writable_schema=RESET)
--    before any later statement in this script.
--
-- 2. clarification_sessions: one row per run parked in 'clarifying' — the
--    2-4 questions asked of the user and (once answered) their answers.
--    answers_json stays NULL until POST /api/runs/{rid}/clarify persists
--    them; both payloads are JSON arrays of strings, caller-encoded.

PRAGMA writable_schema = ON;
UPDATE sqlite_master
  SET sql = replace(sql,
                    '''draft'', ''spec_pending''',
                    '''clarifying'', ''draft'', ''spec_pending''')
  WHERE type = 'table' AND name = 'runs';
PRAGMA writable_schema = RESET;

CREATE TABLE clarification_sessions (
  id            TEXT PRIMARY KEY,
  run_id        TEXT NOT NULL REFERENCES runs(id),
  questions_json TEXT NOT NULL,
  answers_json  TEXT,
  created_at    TEXT NOT NULL
);
CREATE INDEX idx_clarification_run ON clarification_sessions(run_id);
