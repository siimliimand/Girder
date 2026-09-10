-- 013: plan.md §8.4 — approved amendments may widen the amended task's write
-- scope (the most common amendment reason is work needing files outside the
-- declared globs). scope_globs_json stores the globs requested with the
-- amendment (as given by the resolver); on approve they are unioned into
-- tasks.scope_globs_json. Additive only: the column is nullable, so rows
-- written before this migration read back as an empty glob list.

ALTER TABLE spec_amendments ADD COLUMN scope_globs_json TEXT;
