-- Sprint 2 (WP 2.4): the generated proposal is persisted on the run between
-- generation and approval so a crash cannot lose it (plan.md Phase 1).
ALTER TABLE runs ADD COLUMN proposal_md TEXT;
