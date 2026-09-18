-- 007_keyword_discovery.sql
--
-- The two-stage keyword flow.
--
--   STAGE 1  DISCOVER  a `keyword` job scrolls the Ad Library keyword search and
--                      harvests ads WITH each ad's own advertiser page. The server
--                      records the pages it surfaced into a REVIEW LIST. It does
--                      not track them and it never reconciles (ingest enforces).
--   STAGE 2  SCAN      the owner accepts pages from the review list (or turns on
--                      auto-scan) and a normal page_scan job scrapes every ad on
--                      those pages, through job_service.create_job and its
--                      one-page-one-scan duplicate guard.
--
-- Why new tables and not ALTERs: app/db.py replays a migration file whose
-- bookkeeping row never landed, and `ALTER TABLE ... ADD COLUMN` is not
-- re-runnable (duplicate column name). Every statement here is IF NOT EXISTS,
-- so a replay is harmless. 002's keyword_queries / keyword_runs /
-- keyword_results are untouched and keep their v1 meaning.

-- The cross-run review list: one row per (saved search, page). This is what
-- "pages discovered by keyword X" means; keyword_results stays the per-run
-- ranking (v1 parity) and is rebuilt from job_batches after every batch.
CREATE TABLE IF NOT EXISTS keyword_discovered_pages (
    query_id          INTEGER NOT NULL REFERENCES keyword_queries(id) ON DELETE CASCADE,
    page_id           INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    platform_page_id  TEXT    NOT NULL,                -- snapshot: numeric or name:<sha1>
    page_name         TEXT    NOT NULL DEFAULT '',
    identity_kind     TEXT    NOT NULL DEFAULT 'numeric'
        CHECK (identity_kind IN ('numeric','name_hash')),
    first_seen_run_id INTEGER REFERENCES keyword_runs(id) ON DELETE SET NULL,
    last_seen_run_id  INTEGER REFERENCES keyword_runs(id) ON DELETE SET NULL,
    times_seen        INTEGER NOT NULL DEFAULT 1,      -- runs that surfaced it
    ads_seen          INTEGER NOT NULL DEFAULT 0,      -- max matching ads over runs
    review_status     TEXT    NOT NULL DEFAULT 'new'
        CHECK (review_status IN ('new','accepted','ignored','queued','scanned','unscannable')),
    scan_job_id       INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    reviewed_at       TEXT,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    PRIMARY KEY (query_id, page_id)
);
CREATE INDEX IF NOT EXISTS idx_kw_disc_status ON keyword_discovered_pages(query_id, review_status);
CREATE INDEX IF NOT EXISTS idx_kw_disc_page   ON keyword_discovered_pages(page_id);

-- Per-run chain state. Lives beside keyword_runs (1:1) instead of on it so the
-- file stays replay-safe. A row appears the first time a run is materialised.
CREATE TABLE IF NOT EXISTS keyword_run_chain (
    run_id              INTEGER PRIMARY KEY REFERENCES keyword_runs(id) ON DELETE CASCADE,
    results_built_at    TEXT,                          -- last keyword_results rebuild
    ads_skipped_no_page INTEGER NOT NULL DEFAULT 0,    -- ads with no resolvable page
    stage2_job_id       INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    stage2_fired_at     TEXT,                          -- set even when 0 pages qualified
    stage2_note         TEXT    NOT NULL DEFAULT '',   -- why 0 pages, for the run history
    updated_at          TEXT    NOT NULL
);

-- Per-saved-search automation. "Auto-scan" = when a stage-1 run completes,
-- queue stage 2 for every discovered page with ads_seen >= min_ads.
CREATE TABLE IF NOT EXISTS keyword_query_automation (
    query_id    INTEGER PRIMARY KEY REFERENCES keyword_queries(id) ON DELETE CASCADE,
    auto_scan   INTEGER NOT NULL DEFAULT 0,
    min_ads     INTEGER NOT NULL DEFAULT 3,
    updated_at  TEXT    NOT NULL
);
