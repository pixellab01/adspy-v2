-- 003_collector_groups.sql
--
-- A "collector" group fills itself: every time a page finishes a COMPLETE scan
-- (outcome complete / exhausted / empty — the same three that let ingest retire
-- stopped ads), that page is linked into the collector.
--
-- Why the owner asked for it: 227 Naaptol pages carry data up to five days old,
-- so an ad can read "active" here and be long dead on Facebook. Rather than wipe
-- and re-scrape blind, the old snapshot is kept as "Naaptol old" and a fresh,
-- initially EMPTY "Naaptol new" collects each page as it is genuinely re-scanned.
-- The gap between the two group counts is the honest progress bar: 12 of 227 done.
--
-- One column, on the group that DOES the collecting:
--   collect_from_group_id -> the group whose pages it watches (NULL = a normal
--   group, which is every group that existed before this migration).
--
-- ALTER TABLE ... ADD COLUMN is additive and rewrites nothing: existing rows get
-- NULL. 002 is barred from ALTER because it ran against the imported 17,573 ads
-- while the schema was still settling; that ban is specific to 002.

ALTER TABLE groups ADD COLUMN collect_from_group_id INTEGER
    REFERENCES groups(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_groups_collect_from
    ON groups(collect_from_group_id)
    WHERE collect_from_group_id IS NOT NULL;
