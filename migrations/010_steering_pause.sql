-- 010: Sprint 6 (Phase 5) steering + telemetry columns.
--
-- Pause is a pump-level suspend, not an FSM state: the canonical §5.1 run
-- transition table has no `paused` node, so the flag lives beside `status`
-- and the run stays `active` while paused (impl-plan §6.12 "paused"
-- descriptor). A dedicated column keeps the pause durable across daemon
-- restarts — the steering_events row itself is consumed exactly once.
--
-- notifications_log gains run_id so the post-mortem explorer can show the
-- notification trail of a run (notifier already knows the run_id).

ALTER TABLE runs ADD COLUMN paused INTEGER NOT NULL DEFAULT 0;
ALTER TABLE notifications_log ADD COLUMN run_id TEXT;
