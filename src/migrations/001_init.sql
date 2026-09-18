-- 001_init.sql — AdSpy v2 initial schema.
--
-- Source of truth: docs/03-architecture.md §2 (the 16-table minimal schema).
-- Rules baked in here (docs/00-decisions.md):
--   * no workspace_id, no users, no roles, no auth anywhere;
--   * natural keys everywhere (page = platform_page_id, ad = library_id) so
--     both ingest and the v1 importer are idempotent;
--   * no cache/overview/stats tables, no alerts/monitors/trends tables.
--
-- Every statement must be safe to run against an already-migrated database
-- (the runner records applied files, but IF NOT EXISTS keeps a half-applied
-- file recoverable).

-- ---------------------------------------------------------------------------
-- 1. pages  — the advertisers we track (v1: advertiser_pages)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pages (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_page_id     TEXT    NOT NULL,              -- numeric Meta page id, or name:<sha1[:20]> fallback identity
    name                 TEXT    NOT NULL DEFAULT '',
    normalized_name      TEXT    NOT NULL DEFAULT '',
    url                  TEXT,
    alias                TEXT,                          -- owner-supplied display name (v1: page_manual_aliases)
    profile_image_url    TEXT,
    is_tracked           INTEGER NOT NULL DEFAULT 1,
    is_hidden            INTEGER NOT NULL DEFAULT 0,    -- replaces v1 blocklist tables
    active_ads           INTEGER NOT NULL DEFAULT 0,    -- derived: COUNT(ads WHERE status='active')
    total_ads            INTEGER NOT NULL DEFAULT 0,    -- derived: COUNT(ads)
    represented_ads      INTEGER NOT NULL DEFAULT 0,    -- derived: SUM(represented_ad_count) over active ads
    fb_estimated_results INTEGER,                       -- header "~N results" from the last scan
    current_scan_status  TEXT    NOT NULL DEFAULT 'idle'
        CHECK (current_scan_status IN ('idle','queued','running','error')),
    prev_active_ads      INTEGER NOT NULL DEFAULT 0,    -- active_ads before the last completed scan
    last_delta           INTEGER NOT NULL DEFAULT 0,    -- active_ads - prev_active_ads  ("change since last scan")
    last_new_ads         INTEGER NOT NULL DEFAULT 0,    -- ads first seen in the last completed scan
    last_stopped_ads     INTEGER NOT NULL DEFAULT 0,    -- ads deactivated by the last reconciliation
    last_verified_at     TEXT,                          -- last completed page scan (isFinal + good outcome)
    last_captured_at     TEXT,                          -- last time any batch touched this page
    first_captured_at    TEXT,
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL,
    UNIQUE (platform_page_id)
);
CREATE INDEX IF NOT EXISTS idx_pages_tracked      ON pages(is_tracked, is_hidden);
CREATE INDEX IF NOT EXISTS idx_pages_verified     ON pages(last_verified_at);
CREATE INDEX IF NOT EXISTS idx_pages_normalized   ON pages(normalized_name);

-- ---------------------------------------------------------------------------
-- 2. ads  — one row per Ad Library card (v1: ads, minus spend/impressions/currency)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ads (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id              INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    library_id           TEXT    NOT NULL,
    status               TEXT    NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','inactive')),
    start_date           TEXT,                          -- from Meta's "Started running on" label ONLY (R11)
    end_date             TEXT,                          -- set by reconciliation when an ad disappears
    ad_text              TEXT,
    headline             TEXT,
    description          TEXT,
    cta                  TEXT,
    destination_url      TEXT,
    media_type           TEXT,                          -- video | image | carousel | unknown
    media_urls           TEXT    NOT NULL DEFAULT '[]', -- JSON array of strings
    represented_ad_count INTEGER NOT NULL DEFAULT 1,
    content_hash         TEXT    NOT NULL DEFAULT '',
    first_captured_at    TEXT    NOT NULL,
    last_captured_at     TEXT    NOT NULL,
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL,
    UNIQUE (library_id)
);
CREATE INDEX IF NOT EXISTS idx_ads_page_status   ON ads(page_id, status);
CREATE INDEX IF NOT EXISTS idx_ads_status        ON ads(status);
CREATE INDEX IF NOT EXISTS idx_ads_last_captured ON ads(last_captured_at);
CREATE INDEX IF NOT EXISTS idx_ads_start_date    ON ads(start_date);

-- ---------------------------------------------------------------------------
-- 3. ad_versions  — creative history; a new row whenever content_hash changes
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ad_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ad_id           INTEGER NOT NULL REFERENCES ads(id) ON DELETE CASCADE,
    version_number  INTEGER NOT NULL,
    content_hash    TEXT    NOT NULL,
    captured_at     TEXT    NOT NULL,
    ad_text         TEXT,
    headline        TEXT,
    description     TEXT,
    cta             TEXT,
    destination_url TEXT,
    media_urls      TEXT    NOT NULL DEFAULT '[]',
    change_summary  TEXT,
    UNIQUE (ad_id, version_number),
    UNIQUE (ad_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_ad_versions_ad ON ad_versions(ad_id, version_number);

-- ---------------------------------------------------------------------------
-- 4. products  — shortlist_state replaces v1's winners/test_queue twin tables
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_name      TEXT    NOT NULL,
    display_name         TEXT    NOT NULL DEFAULT '',
    product_url          TEXT,
    domain               TEXT,
    shortlist_state      TEXT
        CHECK (shortlist_state IS NULL
               OR shortlist_state IN ('shortlisted','testing','winner','killed')),
    shortlist_updated_at TEXT,
    first_seen_at        TEXT    NOT NULL,
    last_seen_at         TEXT    NOT NULL,
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL,
    UNIQUE (normalized_name)
);
CREATE INDEX IF NOT EXISTS idx_products_shortlist ON products(shortlist_state);
CREATE INDEX IF NOT EXISTS idx_products_domain    ON products(domain);

-- ---------------------------------------------------------------------------
-- 5. ad_products
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ad_products (
    ad_id      INTEGER NOT NULL REFERENCES ads(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    method     TEXT    NOT NULL DEFAULT 'url',   -- url | manual | llm
    confidence REAL    NOT NULL DEFAULT 1.0,
    created_at TEXT    NOT NULL,
    PRIMARY KEY (ad_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_ad_products_product ON ad_products(product_id);

-- ---------------------------------------------------------------------------
-- 6. transcripts  — v1 creative_transcripts, with the links table folded out
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transcripts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    media_url_hash TEXT    NOT NULL,            -- sha256 of media URL minus query  (dedupe layer 1)
    media_url      TEXT,
    content_sha256 TEXT,                        -- sha256 of downloaded bytes       (dedupe layer 2)
    status         TEXT    NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','processing','completed','failed')),
    language       TEXT,
    text           TEXT,
    hook_summary   TEXT,
    low_confidence INTEGER NOT NULL DEFAULT 0,
    cluster_id     INTEGER REFERENCES script_clusters(id) ON DELETE SET NULL,
    model          TEXT,
    provider       TEXT,
    error          TEXT,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL,
    UNIQUE (media_url_hash)
);
CREATE INDEX IF NOT EXISTS idx_transcripts_status  ON transcripts(status);
CREATE INDEX IF NOT EXISTS idx_transcripts_content ON transcripts(content_sha256);
CREATE INDEX IF NOT EXISTS idx_transcripts_cluster ON transcripts(cluster_id);

-- ---------------------------------------------------------------------------
-- 7. transcript_ads  — v1 creative_transcript_links
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transcript_ads (
    transcript_id INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
    ad_id         INTEGER NOT NULL REFERENCES ads(id) ON DELETE CASCADE,
    link_source   TEXT    NOT NULL DEFAULT 'url_hash'
        CHECK (link_source IN ('url_hash','content_hash','cluster')),
    created_at    TEXT    NOT NULL,
    PRIMARY KEY (transcript_id, ad_id)
);
CREATE INDEX IF NOT EXISTS idx_transcript_ads_ad ON transcript_ads(ad_id);

-- ---------------------------------------------------------------------------
-- 8. script_clusters  — v1 creative_script_clusters
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS script_clusters (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    language                     TEXT    NOT NULL DEFAULT 'und',
    representative_transcript_id INTEGER,      -- FK omitted on purpose: circular with transcripts
    signature                    TEXT,
    canonical_length             INTEGER,
    member_count                 INTEGER NOT NULL DEFAULT 0,
    algo_version                 INTEGER NOT NULL DEFAULT 1,
    created_at                   TEXT    NOT NULL,
    updated_at                   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_script_clusters_lang ON script_clusters(language);

-- ---------------------------------------------------------------------------
-- 9. groups  — v1 brand_groups (name only; stats are always live queries)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS "groups" (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    notes      TEXT,
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL,
    UNIQUE (name)
);

-- ---------------------------------------------------------------------------
-- 10. group_pages
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS group_pages (
    group_id INTEGER NOT NULL REFERENCES "groups"(id) ON DELETE CASCADE,
    page_id  INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    added_at TEXT    NOT NULL,
    PRIMARY KEY (group_id, page_id)
);
CREATE INDEX IF NOT EXISTS idx_group_pages_page ON group_pages(page_id);

-- ---------------------------------------------------------------------------
-- 11. jobs  — the v15 lease/claim core, minus the fleet machinery
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type            TEXT    NOT NULL
        CHECK (job_type IN ('page_scan','keyword')),
    status              TEXT    NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','claimed','running','completed','failed','cancelled')),
    idempotency_key     TEXT    NOT NULL,
    label               TEXT,
    lease_token_hash    TEXT,
    lease_expires_at    TEXT,
    claimed_at          TEXT,
    started_at          TEXT,
    finished_at         TEXT,
    outcome             TEXT,                            -- completed | failed
    error               TEXT,
    error_code          TEXT,                            -- FB_LOGIN_WALL | FB_CAPTCHA | FB_BLOCKED | ...
    retryable           INTEGER NOT NULL DEFAULT 0,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    max_retries         INTEGER NOT NULL DEFAULT 3,
    targets_total       INTEGER NOT NULL DEFAULT 0,
    targets_done        INTEGER NOT NULL DEFAULT 0,
    cancel_requested_at TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    UNIQUE (idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status, id);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);

-- ---------------------------------------------------------------------------
-- 12. job_targets  — per-target live progress (real columns, never a JSON blob)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_targets (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id            INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    position          INTEGER NOT NULL,
    page_id           INTEGER REFERENCES pages(id) ON DELETE SET NULL,
    platform_page_id  TEXT,
    page_url          TEXT,
    label             TEXT,                              -- page name, or the query for keyword jobs
    status            TEXT    NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','done','failed','skipped')),
    outcome           TEXT
        CHECK (outcome IS NULL
               OR outcome IN ('complete','exhausted','empty','partial','blocked','failed')),
    scrolls           INTEGER NOT NULL DEFAULT 0,
    unique_ads        INTEGER NOT NULL DEFAULT 0,
    represented_ads   INTEGER NOT NULL DEFAULT 0,
    estimated_results INTEGER NOT NULL DEFAULT 0,
    message           TEXT,
    started_at        TEXT,
    finished_at       TEXT,
    UNIQUE (job_id, position)
);
CREATE INDEX IF NOT EXISTS idx_job_targets_page   ON job_targets(page_id);
CREATE INDEX IF NOT EXISTS idx_job_targets_status ON job_targets(status);

-- ---------------------------------------------------------------------------
-- 13. job_batches  — batch receipt + idempotency key, one table (v1 used five)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_batches (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id               INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    batch_id             TEXT    NOT NULL,               -- client-generated (R9)
    batch_sequence       INTEGER NOT NULL DEFAULT 0,
    target_position      INTEGER,
    page_id              INTEGER REFERENCES pages(id) ON DELETE SET NULL,
    is_final             INTEGER NOT NULL DEFAULT 0,
    outcome              TEXT,
    status               TEXT    NOT NULL DEFAULT 'accepted'
        CHECK (status IN ('accepted','rejected')),
    ads_seen             INTEGER NOT NULL DEFAULT 0,
    ads_new              INTEGER NOT NULL DEFAULT 0,
    ads_updated          INTEGER NOT NULL DEFAULT 0,
    ads_deactivated      INTEGER NOT NULL DEFAULT 0,
    represented_ad_count INTEGER NOT NULL DEFAULT 0,
    payload_hash         TEXT,
    accepted_ad_ids_json TEXT    NOT NULL DEFAULT '[]',  -- library_ids accepted; the job-union for reconciliation
    receipt_json         TEXT    NOT NULL DEFAULT '{}',  -- verbatim response replayed on duplicate
    received_at          TEXT    NOT NULL,
    UNIQUE (job_id, batch_id)
);
CREATE INDEX IF NOT EXISTS idx_job_batches_job_page ON job_batches(job_id, page_id, status);

-- ---------------------------------------------------------------------------
-- 14. page_daily_metrics  — 7D sparkline + the delta column's history
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS page_daily_metrics (
    page_id         INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    metric_date     TEXT    NOT NULL,          -- YYYY-MM-DD (UTC)
    active_ads      INTEGER NOT NULL DEFAULT 0,
    new_ads         INTEGER NOT NULL DEFAULT 0,
    stopped_ads     INTEGER NOT NULL DEFAULT 0,
    represented_ads INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (page_id, metric_date)
);
CREATE INDEX IF NOT EXISTS idx_page_daily_date ON page_daily_metrics(metric_date);

-- ---------------------------------------------------------------------------
-- 15. product_daily_metrics
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_daily_metrics (
    product_id       INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    metric_date      TEXT    NOT NULL,
    ad_volume        INTEGER NOT NULL DEFAULT 0,
    advertiser_count INTEGER NOT NULL DEFAULT 0,
    new_ads          INTEGER NOT NULL DEFAULT 0,
    momentum         REAL    NOT NULL DEFAULT 0,
    winner_score     REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (product_id, metric_date)
);
CREATE INDEX IF NOT EXISTS idx_product_daily_date ON product_daily_metrics(metric_date);

-- ---------------------------------------------------------------------------
-- 16. settings  — one key/value table replaces v1's six singleton tables
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
