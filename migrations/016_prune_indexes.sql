-- 016: (status, created_at) index on runs for the prune query (impl-plan WP 13.2).
-- ``girder prune`` selects runs by terminal status past an age horizon; without
-- this index the query is a full scan of runs on every invocation.
-- Additive only: idempotent (IF NOT EXISTS), matching house style.

CREATE INDEX IF NOT EXISTS idx_runs_status_created ON runs(status, created_at);
