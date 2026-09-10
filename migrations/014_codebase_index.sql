-- 014: Sprint 8 (WP 8.4) — codebase index ("RAG Lite").
--
-- One JSON blob per task: the structural index (file tree, Python symbol
-- map, import graph) of the attempt worktree, built fresh at every attempt
-- start (R-SP8-4: never cached across attempts — worktree state changes
-- between attempts, and a stale index is worse than no index). The blob is
-- diagnostic/persistence data; the agent-facing slice is rendered from it at
-- inject time by girder.index.inject. Additive only: the column is nullable,
-- so tasks written before this migration (or with inject_index disabled)
-- read back as NULL.

ALTER TABLE tasks ADD COLUMN codebase_index_json TEXT;
