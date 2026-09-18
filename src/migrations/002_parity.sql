-- 002_parity.sql — everything the v1 screens need that 001 did not have.
--
-- WHY THIS FILE EXISTS
-- 001_init.sql was written for a four-screen tool. The owner then asked for
-- v1's whole surface back (docs/00-decisions.md, "संशोधन — 2026-08-03"), so
-- Alerts, Sessions, Keyword Research, Products, Brand Groups, Winners, Test
-- Queue, Settings and the manual Queue all need storage. This file adds it.
--
-- THREE RULES, ALL LOAD-BEARING
--
--   1. ADDITIVE ONLY — NOT ONE `ALTER TABLE`.
--      001's tables hold the owner's imported 17,573 ads. Nothing here
--      touches them. Where a screen needs a new field on an existing row
--      (a page's logical key, a product's four flags, a group's domain) it
--      goes in a side table keyed 1:1 on the parent id, and a view at the
--      bottom of this file joins it back on with sane defaults. That costs
--      one join and buys a migration that cannot corrupt anything.
--
--   2. IDEMPOTENT. Every statement is CREATE ... IF NOT EXISTS, because the
--      runner (app/db.py:run_migrations) executes the file first and records
--      it second — a crash in between replays the whole file.
--
--   3. NO workspace_id, NO users, NO roles (decision #4). v1's columns
--      `workspace_id`, `user_id`, `created_by`, `buyer_user_id`,
--      `blocked_by`, `removed_by` are all dropped on the way in. Where v1
--      pointed at a person, v2 keeps a free-text name or nothing at all —
--      tests/test_end_to_end.py::test_no_table_carries_a_workspace_or_user_column
--      fails the build if one creeps back.
--
-- Natural keys throughout, so the v1 importer stays idempotent:
--   page = platform_page_id · ad = library_id · product = normalized_name
--   keyword run = (query, requested_at) · test queue entry = (group, product)


-- ===========================================================================
-- A. SIDE TABLES ON 001's ROWS
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- A1. page_identity — the logical page (gap G1, the biggest one).
--
-- v1 collapses many advertiser_pages rows into one *logical* page by name:
-- Naaptol is 250 source records but 83 logical pages. Brand Groups' Pages tab,
-- its "Sources" column, the page drawer's source list, the multi-source
-- "Delete..." picker and the logical re-track all read this; Winners' *Pages*
-- metric IS the logical count and three of its six formulas are built on it.
--
-- logical_key is v1's `name:<sha1[:20]>` of the normalized name. Pages with no
-- row here are their own logical page — see v_page_identity.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS page_identity (
    page_id       INTEGER PRIMARY KEY REFERENCES pages(id) ON DELETE CASCADE,
    logical_key   TEXT    NOT NULL,
    display_name  TEXT,                              -- the logical page's name
    is_primary    INTEGER NOT NULL DEFAULT 0,        -- the source record we link to
    source_rank   INTEGER NOT NULL DEFAULT 0,        -- ordering inside the logical page
    updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_page_identity_key ON page_identity(logical_key, is_primary DESC, source_rank);

-- ---------------------------------------------------------------------------
-- A2. page_states — the four flags v1 kept in page_analyzer_* tables.
-- pages.is_tracked / is_hidden already exist in 001; these are the two that do
-- not, plus a "removed" tombstone so a page deleted by hand is not resurrected
-- by the next scrape (v1: page_analyzer_removed_pages).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS page_states (
    page_id      INTEGER PRIMARY KEY REFERENCES pages(id) ON DELETE CASCADE,
    is_saved     INTEGER NOT NULL DEFAULT 0,   -- Products screen: "Saved advertiser pages"
    is_favorite  INTEGER NOT NULL DEFAULT 0,   -- Overview: pinned strip
    is_removed   INTEGER NOT NULL DEFAULT 0,
    saved_at     TEXT,
    favorite_at  TEXT,
    removed_at   TEXT,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_page_states_saved    ON page_states(is_saved)    WHERE is_saved = 1;
CREATE INDEX IF NOT EXISTS idx_page_states_favorite ON page_states(is_favorite) WHERE is_favorite = 1;

-- ---------------------------------------------------------------------------
-- A3. page_scan_history — FB-results freshness and cumulative stopped ads.
--
-- 001 keeps only the latest reading (pages.fb_estimated_results,
-- pages.last_stopped_ads). Brand Groups' FB-results column needs the *age* of
-- the reading to colour it (>7d amber, >30d red) and to print "12 Jul - 4d
-- ago"; the page drawer's "Stopped" tile needs the running total, not the last
-- scan's. One row per completed scan gives both.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS page_scan_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id           INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    job_id            INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    batch_id          TEXT,
    active_unique_ads INTEGER NOT NULL DEFAULT 0,
    represented_ads   INTEGER NOT NULL DEFAULT 0,
    estimated_results INTEGER,                       -- Facebook's own "~N results"
    new_ads           INTEGER NOT NULL DEFAULT 0,
    stopped_ads       INTEGER NOT NULL DEFAULT 0,
    source            TEXT    NOT NULL DEFAULT 'extension',
    captured_at       TEXT    NOT NULL,
    UNIQUE (page_id, job_id, batch_id)
);
CREATE INDEX IF NOT EXISTS idx_page_scan_history_page ON page_scan_history(page_id, captured_at DESC);

-- ---------------------------------------------------------------------------
-- A4. ad_languages — one language per ad (gap G2).
--
-- 001 stores `language` only on transcripts, and transcript_ads only links ads
-- that were actually transcribed — so 9,658 of v1's classified ads would have
-- no language in v2. The Brand Groups drawer needs it for the Language column,
-- the language chips, the `und` bucket, "Detect languages" and
-- generate-scripts-by-language. Cheap text detection must be able to fill this
-- WITHOUT transcription, which is exactly why it is not on transcripts.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ad_languages (
    ad_id            INTEGER PRIMARY KEY REFERENCES ads(id) ON DELETE CASCADE,
    text_language    TEXT,
    text_confidence  REAL,
    text_method      TEXT,                           -- heuristic | model | manual
    media_language   TEXT,                           -- from the transcript, when there is one
    final_language   TEXT NOT NULL DEFAULT 'und',
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ad_languages_final ON ad_languages(final_language);

-- ---------------------------------------------------------------------------
-- A5. product_states — four INDEPENDENT flags (gap G6).
--
-- 001's products.shortlist_state is one mutually-exclusive column; v1 has four
-- booleans that a product carries at once, and they drive four of the seven
-- Products tabs plus four buttons in three different drawers. A product can be
-- tracked AND saved AND favourite. shortlist_state stays where it is and keeps
-- meaning "where in the decision pipeline", which is a different question.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_states (
    product_id  INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    tracked     INTEGER NOT NULL DEFAULT 0,
    saved       INTEGER NOT NULL DEFAULT 0,
    favorite    INTEGER NOT NULL DEFAULT 0,
    hidden      INTEGER NOT NULL DEFAULT 0,
    tracked_at  TEXT,
    saved_at    TEXT,
    favorite_at TEXT,
    hidden_at   TEXT,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_product_states_tracked  ON product_states(tracked)  WHERE tracked = 1;
CREATE INDEX IF NOT EXISTS idx_product_states_saved    ON product_states(saved)    WHERE saved = 1;
CREATE INDEX IF NOT EXISTS idx_product_states_favorite ON product_states(favorite) WHERE favorite = 1;
CREATE INDEX IF NOT EXISTS idx_product_states_hidden   ON product_states(hidden)   WHERE hidden = 1;

-- ---------------------------------------------------------------------------
-- A6. product_meta — the fields the Products drawer prints and Winners filters
-- on. identity_status is Winners' `#winIdentity` control (Safe / Confirmed /
-- Candidate / Unknown / Conflict) and the multiplier in the complex formula
-- (confirmed 1.00 · candidate 0.90 · unknown 0.75 · conflict 0.45). With no
-- row here a product is 'unknown' — see v_product.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_meta (
    product_id      INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    category        TEXT,
    product_type    TEXT,
    canonical_url   TEXT,
    store_platform  TEXT,                            -- shopify | woocommerce | ...
    identity_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (identity_status IN ('confirmed','candidate','unknown','conflict')),
    identity_note   TEXT,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_product_meta_identity ON product_meta(identity_status);

-- ---------------------------------------------------------------------------
-- A7. product_removed — "Remove permanently" must survive the next scrape.
-- Keyed on an identity hash rather than the product id, because the row it
-- refers to is gone.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_removed (
    identity_hash   TEXT PRIMARY KEY,                -- sha1(canonical_url or normalized_name)
    normalized_name TEXT,
    canonical_url   TEXT,
    domain          TEXT,
    display_name    TEXT,
    removed_at      TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- A8. group_meta / group_page_meta — the group fields the Edit modal writes
-- and the page drawer reads. 001's groups(name, notes) is missing the primary
-- domain (the detail sub-heading), the category and the avatar colour;
-- group_pages(added_at) is missing the " - primary" marker on a source row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS group_meta (
    group_id       INTEGER PRIMARY KEY REFERENCES "groups"(id) ON DELETE CASCADE,
    primary_domain TEXT,
    category       TEXT,
    color_key      TEXT,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_page_meta (
    group_id          INTEGER NOT NULL REFERENCES "groups"(id) ON DELETE CASCADE,
    page_id           INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    is_primary_page   INTEGER NOT NULL DEFAULT 0,
    relationship_type TEXT    NOT NULL DEFAULT 'owned',
    source            TEXT    NOT NULL DEFAULT 'manual',   -- manual | auto | import
    confidence        REAL    NOT NULL DEFAULT 1.0,
    removed_at        TEXT,
    PRIMARY KEY (group_id, page_id)
);
CREATE INDEX IF NOT EXISTS idx_group_page_meta_page ON group_page_meta(page_id);


-- ===========================================================================
-- B. NEW SCREENS
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- B1. ALERTS  (/alerts — "Scaling Alerts")
--
-- Dashboard-only: decision #6 killed Telegram and email, so there is no
-- delivery state here, just read_at. The thresholds
-- (scaling_threshold_pct / _abs, new_hot_page_min_ads, new_hot_page_window_days,
-- product_surge_min_ads, product_surge_min_advertisers, alerts_enabled) live in
-- 001's settings table as `alerts.*` keys — one k/v table replaces v1's six
-- singleton config tables.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type    TEXT    NOT NULL CHECK (entity_type IN ('page','product','group')),
    entity_id      INTEGER NOT NULL,
    alert_type     TEXT    NOT NULL,                 -- scaling | new_hot_page | product_surge | stopped
    severity       TEXT    NOT NULL DEFAULT 'info' CHECK (severity IN ('info','warn','high')),
    message        TEXT    NOT NULL,
    current_value  TEXT,
    previous_value TEXT,
    dedupe_key     TEXT    NOT NULL,                 -- entity+type+day: one alert per fact per day
    generated_at   TEXT    NOT NULL,
    read_at        TEXT,
    resolved_at    TEXT,
    UNIQUE (dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_alerts_feed   ON alerts(generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_unread ON alerts(read_at, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_entity ON alerts(entity_type, entity_id);

-- What each page looked like when the alert sweep last ran, so "just started
-- scaling" compares against a fixed point instead of the previous scan.
CREATE TABLE IF NOT EXISTS alert_page_baselines (
    page_id     INTEGER PRIMARY KEY REFERENCES pages(id) ON DELETE CASCADE,
    active_ads  INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT    NOT NULL
);

-- ---------------------------------------------------------------------------
-- B2. SESSIONS  (/sessions)
--
-- An extraction boundary: "everything the extension sent me between Start and
-- Stop". The Sessions screen watches accepted server data arrive against it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_number   INTEGER NOT NULL,
    name             TEXT    NOT NULL,
    keyword          TEXT    NOT NULL DEFAULT '',
    notes            TEXT    NOT NULL DEFAULT '',
    status           TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active','complete')),
    pages_seen       INTEGER NOT NULL DEFAULT 0,
    ads_seen         INTEGER NOT NULL DEFAULT 0,
    represented_ads  INTEGER NOT NULL DEFAULT 0,
    started_at       TEXT    NOT NULL,
    last_activity_at TEXT,
    completed_at     TEXT,
    created_at       TEXT    NOT NULL,
    UNIQUE (session_number)
);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status, started_at DESC);

CREATE TABLE IF NOT EXISTS session_pages (
    session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    page_id         INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    first_seen_at   TEXT    NOT NULL,
    last_seen_at    TEXT    NOT NULL,
    scrape_count    INTEGER NOT NULL DEFAULT 1,
    active_ads      INTEGER NOT NULL DEFAULT 0,
    represented_ads INTEGER NOT NULL DEFAULT 0,
    meta_results    INTEGER,
    PRIMARY KEY (session_id, page_id)
);
CREATE INDEX IF NOT EXISTS idx_session_pages_page ON session_pages(page_id);

CREATE TABLE IF NOT EXISTS session_ads (
    session_id    INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ad_id         INTEGER NOT NULL REFERENCES ads(id) ON DELETE CASCADE,
    page_id       INTEGER REFERENCES pages(id) ON DELETE SET NULL,
    first_seen_at TEXT    NOT NULL,
    last_seen_at  TEXT    NOT NULL,
    seen_count    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (session_id, ad_id)
);
CREATE INDEX IF NOT EXISTS idx_session_ads_ad ON session_ads(ad_id);

-- The arrival log the screen tails.
CREATE TABLE IF NOT EXISTS session_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    batch_id          TEXT,
    source            TEXT    NOT NULL DEFAULT 'extension',
    keyword           TEXT    NOT NULL DEFAULT '',
    page_count        INTEGER NOT NULL DEFAULT 0,
    ads_received      INTEGER NOT NULL DEFAULT 0,
    ads_processed     INTEGER NOT NULL DEFAULT 0,
    represented_ads   INTEGER NOT NULL DEFAULT 0,
    snapshot_complete INTEGER NOT NULL DEFAULT 0,
    page_summary      TEXT    NOT NULL DEFAULT '',
    created_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_events_session ON session_events(session_id, id DESC);

-- ---------------------------------------------------------------------------
-- B3. KEYWORD RESEARCH  (/keyword-research)
--
-- Decision #2 parked this in Phase 4; the parity reversal brought it back.
-- A saved search (keyword_queries) is run over and over (keyword_runs), and
-- each run ranks the pages it found (keyword_results). A run rides on a normal
-- job from 001 — keyword is already in jobs.job_type's CHECK — so there is no
-- second queue here.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS keyword_queries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword       TEXT    NOT NULL,
    country       TEXT    NOT NULL DEFAULT 'IN',
    ad_status     TEXT    NOT NULL DEFAULT 'active',
    platform      TEXT    NOT NULL DEFAULT 'all',
    media_type    TEXT    NOT NULL DEFAULT 'all',
    default_depth INTEGER NOT NULL DEFAULT 500,
    max_pages     INTEGER NOT NULL DEFAULT 100,
    sort_mode     TEXT    NOT NULL DEFAULT 'relevance',
    is_saved      INTEGER NOT NULL DEFAULT 0,        -- the "Favorites" tab
    is_favorite   INTEGER NOT NULL DEFAULT 0,
    last_run_at   TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    UNIQUE (keyword, country, ad_status, platform, media_type)
);
CREATE INDEX IF NOT EXISTS idx_keyword_queries_saved ON keyword_queries(is_saved, last_run_at DESC);

CREATE TABLE IF NOT EXISTS keyword_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id           INTEGER NOT NULL REFERENCES keyword_queries(id) ON DELETE CASCADE,
    job_id             INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    status             TEXT    NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','completed','failed','cancelled')),
    ads_scanned        INTEGER NOT NULL DEFAULT 0,
    unique_library_ids INTEGER NOT NULL DEFAULT 0,
    represented_ads    INTEGER NOT NULL DEFAULT 0,
    unique_pages       INTEGER NOT NULL DEFAULT 0,
    scroll_count       INTEGER NOT NULL DEFAULT 0,
    duplicate_count    INTEGER NOT NULL DEFAULT 0,
    error_count        INTEGER NOT NULL DEFAULT 0,
    duration_seconds   REAL    NOT NULL DEFAULT 0,
    stop_reason        TEXT,
    requested_at       TEXT    NOT NULL,
    started_at         TEXT,
    finished_at        TEXT,
    UNIQUE (query_id, requested_at)
);
CREATE INDEX IF NOT EXISTS idx_keyword_runs_query  ON keyword_runs(query_id, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_keyword_runs_status ON keyword_runs(status);

CREATE TABLE IF NOT EXISTS keyword_results (
    run_id                  INTEGER NOT NULL REFERENCES keyword_runs(id) ON DELETE CASCADE,
    page_id                 INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    ads_matching            INTEGER NOT NULL DEFAULT 0,
    represented_matching    INTEGER NOT NULL DEFAULT 0,
    total_active_page_ads   INTEGER NOT NULL DEFAULT 0,
    top_product             TEXT,
    oldest_matching_ad_date TEXT,
    rank_position           INTEGER,
    first_result_position   INTEGER,
    selected_for_analysis   INTEGER NOT NULL DEFAULT 0,
    analysis_status         TEXT    NOT NULL DEFAULT 'not_selected',
    PRIMARY KEY (run_id, page_id)
);
CREATE INDEX IF NOT EXISTS idx_keyword_results_rank ON keyword_results(run_id, rank_position);
CREATE INDEX IF NOT EXISTS idx_keyword_results_page ON keyword_results(page_id);

-- ---------------------------------------------------------------------------
-- B4. PRODUCTS — page sets and the extract-all run (Products tabs 2 and the
-- Extract All progress bar).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_page_sets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (name)
);

CREATE TABLE IF NOT EXISTS product_page_set_items (
    set_id     INTEGER NOT NULL REFERENCES product_page_sets(id) ON DELETE CASCADE,
    page_id    INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    added_at   TEXT    NOT NULL,
    PRIMARY KEY (set_id, page_id)
);
CREATE INDEX IF NOT EXISTS idx_product_page_set_items_page ON product_page_set_items(page_id);

CREATE TABLE IF NOT EXISTS product_extraction_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    status           TEXT    NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','processing','completed','failed','cancelled')),
    total_ads        INTEGER NOT NULL DEFAULT 0,
    processed_ads    INTEGER NOT NULL DEFAULT 0,
    products_found   INTEGER NOT NULL DEFAULT 0,
    products_created INTEGER NOT NULL DEFAULT 0,
    links_created    INTEGER NOT NULL DEFAULT 0,
    skipped_ads      INTEGER NOT NULL DEFAULT 0,
    progress_message TEXT,
    error_message    TEXT,
    created_at       TEXT    NOT NULL,
    started_at       TEXT,
    heartbeat_at     TEXT,
    completed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_product_extraction_runs_status ON product_extraction_runs(status, id DESC);

-- ---------------------------------------------------------------------------
-- B5. WINNERS  (/winners)
--
-- winner_shortlist is v1's winner_submissions with the two user columns gone:
-- one row per (group, product) the owner has judged. `assigned_to` is free
-- text — there is no user list to populate a select from (decision #4), so the
-- control is a text field, not a dropdown that loads nothing.
--
-- The per-formula scores themselves are NOT stored: they are live SQL over
-- ads.start_date / ads.media_type. Naaptol is 837 products against 11.6k
-- active ads per ranking request, so winner_rank_snapshots caches the answer
-- per group and the screen serves the snapshot while a rebuild runs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS winner_shortlist (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id      INTEGER NOT NULL REFERENCES "groups"(id) ON DELETE CASCADE,
    product_id    INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    winner_status TEXT    NOT NULL DEFAULT 'watch'
        CHECK (winner_status IN ('proven','rising','watch','weak')),
    score_mode    TEXT    NOT NULL DEFAULT 'simple' CHECK (score_mode IN ('simple','complex')),
    score_formula TEXT    NOT NULL DEFAULT 'overall'
        CHECK (score_formula IN ('overall','scaled','breakout','evergreen','replicated','revival')),
    score_value   REAL    NOT NULL DEFAULT 0,
    score_display TEXT,
    assigned_to   TEXT,                              -- free text: no accounts in v2
    target_cpp    REAL    NOT NULL DEFAULT 0,
    submitted_at  TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    UNIQUE (group_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_winner_shortlist_group ON winner_shortlist(group_id, score_value DESC);

CREATE TABLE IF NOT EXISTS winner_rank_snapshots (
    group_id        INTEGER NOT NULL REFERENCES "groups"(id) ON DELETE CASCADE,
    score_mode      TEXT    NOT NULL,
    score_formula   TEXT    NOT NULL,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    payload_json    TEXT    NOT NULL DEFAULT '[]',
    generated_at    TEXT    NOT NULL,
    PRIMARY KEY (group_id, score_mode, score_formula)
);

-- ---------------------------------------------------------------------------
-- B6. LANDING INTELLIGENCE  (Winners -> "Extract details", gap G4)
--
-- A capture run walks a group's ranked products, opens each landing page and
-- records what it sells for. The Winners dialog's section 3 reads the newest
-- successful job per product.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS landing_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id           INTEGER NOT NULL REFERENCES "groups"(id) ON DELETE CASCADE,
    run_scope          TEXT    NOT NULL DEFAULT 'top',   -- top | all | retry
    status             TEXT    NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','stopped','completed','failed')),
    total_products     INTEGER NOT NULL DEFAULT 0,
    completed_products INTEGER NOT NULL DEFAULT 0,
    success_products   INTEGER NOT NULL DEFAULT 0,
    failed_products    INTEGER NOT NULL DEFAULT 0,
    skipped_products   INTEGER NOT NULL DEFAULT 0,
    stop_requested     INTEGER NOT NULL DEFAULT 0,
    error_message      TEXT,
    created_at         TEXT    NOT NULL,
    started_at         TEXT,
    finished_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_landing_runs_group ON landing_runs(group_id, id DESC);

CREATE TABLE IF NOT EXISTS landing_jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL REFERENCES landing_runs(id) ON DELETE CASCADE,
    product_id     INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    queue_position INTEGER NOT NULL DEFAULT 0,
    priority_tier  INTEGER NOT NULL DEFAULT 3,
    status         TEXT    NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','captured','failed','skipped')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    input_url      TEXT,
    final_url      TEXT,
    http_status    INTEGER,
    page_title     TEXT,
    store_platform TEXT,                             -- shopify | woocommerce | custom
    checkout_stack TEXT,
    funnel_type    TEXT,                             -- single product | listing | quiz
    listed_price   REAL,
    selling_price  REAL,
    currency       TEXT,
    discount_text  TEXT,
    offer_text     TEXT,
    availability   TEXT,
    description    TEXT,
    image_urls     TEXT    NOT NULL DEFAULT '[]',    -- JSON array
    screenshot_path TEXT,
    error_code     TEXT,
    error_message  TEXT,
    elapsed_ms     INTEGER NOT NULL DEFAULT 0,
    queued_at      TEXT,
    started_at     TEXT,
    finished_at    TEXT,
    UNIQUE (run_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_landing_jobs_run     ON landing_jobs(run_id, queue_position);
CREATE INDEX IF NOT EXISTS idx_landing_jobs_product ON landing_jobs(product_id, finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_landing_jobs_status  ON landing_jobs(status);

-- ---------------------------------------------------------------------------
-- B7. TEST QUEUE  (/test-queue, gap G3)
--
-- The board has FIVE columns; products.shortlist_state has four values and is
-- one column on a product, so it cannot carry a per-entry target CPP, a buyer,
-- five stage timestamps or a score snapshot. One table, soft delete included —
-- v1 needed three (items + entries + tombstones) because it also had to sync
-- them across a workspace.
--
-- The score columns are a SNAPSHOT taken at push time. That is the point: the
-- board has to show what the product looked like when the decision was made,
-- not what it looks like now.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS test_queue_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id            INTEGER NOT NULL REFERENCES "groups"(id) ON DELETE CASCADE,
    product_id          INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    product_name        TEXT    NOT NULL,
    group_name          TEXT,
    product_domain      TEXT,
    external_product_url TEXT,
    source_page_id      INTEGER REFERENCES pages(id) ON DELETE SET NULL,
    source_page_name    TEXT,
    buyer_name          TEXT,                        -- free text, no accounts
    target_cpp          REAL    NOT NULL DEFAULT 0,
    queue_status        TEXT    NOT NULL DEFAULT 'queued'
        CHECK (queue_status IN ('queued','testing','running','winner','killed')),
    -- evidence, frozen at push time
    simple_score        REAL    NOT NULL DEFAULT 0,
    complex_score       REAL    NOT NULL DEFAULT 0,
    winner_status       TEXT,
    active_ads          INTEGER NOT NULL DEFAULT 0,
    represented_ads     INTEGER NOT NULL DEFAULT 0,
    logical_advertisers INTEGER NOT NULL DEFAULT 0,
    oldest_active_days  INTEGER NOT NULL DEFAULT 0,
    new_ads_30d         INTEGER NOT NULL DEFAULT 0,
    -- stage clock
    queued_at           TEXT,
    testing_at          TEXT,
    running_at          TEXT,
    winner_at           TEXT,
    killed_at           TEXT,
    deleted_at          TEXT,                        -- soft delete; the "Deleted" view
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    UNIQUE (group_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_test_queue_board   ON test_queue_items(queue_status, updated_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_test_queue_deleted ON test_queue_items(deleted_at) WHERE deleted_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_test_queue_product ON test_queue_items(product_id);

-- ---------------------------------------------------------------------------
-- B8. THE MANUAL QUEUE + WORKERS  (/queue, gap G5)
--
-- 001's jobs/job_targets/job_batches already cover the job list, per-target
-- progress, error_code grouping and every health tile. What is missing is the
-- layer above: the hand-built list of things to run, the Start/Stop switch,
-- the Dismiss button and the worker chips.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS queue_source_items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type       TEXT    NOT NULL CHECK (source_type IN ('research','analyze','tracked','manual')),
    source_id         TEXT    NOT NULL,              -- the originating row's id, as text
    label             TEXT    NOT NULL,
    target_key        TEXT    NOT NULL,              -- platform_page_id or the keyword
    target_url        TEXT    NOT NULL DEFAULT '',
    payload_json      TEXT    NOT NULL DEFAULT '{}',
    position          INTEGER NOT NULL DEFAULT 0,
    job_id            INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    source_present    INTEGER NOT NULL DEFAULT 1,    -- cleared when "Sync sources" no longer finds it
    last_synced_at    TEXT,
    excluded_at       TEXT,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    UNIQUE (source_type, source_id)
);
CREATE INDEX IF NOT EXISTS idx_queue_source_items_order ON queue_source_items(position, id);

-- One row, dispatch_group = 'manual'. Start/Stop and the 5-minute gap.
CREATE TABLE IF NOT EXISTS queue_dispatch_state (
    dispatch_group    TEXT PRIMARY KEY,
    is_running        INTEGER NOT NULL DEFAULT 0,
    delay_seconds     INTEGER NOT NULL DEFAULT 300,
    active_job_id     INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    last_completed_at TEXT,
    started_at        TEXT,
    stopped_at        TEXT,
    updated_at        TEXT NOT NULL
);

-- "Dismiss" on the scrape-problems panel: hide this error code until it recurs.
CREATE TABLE IF NOT EXISTS queue_error_acks (
    error_code TEXT PRIMARY KEY,
    acked_at   TEXT NOT NULL
);

-- The worker chips, and the Manual / Scraper / Disabled selector on each.
-- v1's `workers` table has 60 columns of fleet machinery; this is the ten the
-- screen actually renders.
CREATE TABLE IF NOT EXISTS workers (
    worker_id         TEXT PRIMARY KEY,              -- natural key from the extension
    display_name      TEXT    NOT NULL DEFAULT '',
    assigned_mode     TEXT    NOT NULL DEFAULT 'manual'
        CHECK (assigned_mode IN ('manual','scraper','disabled')),
    status            TEXT    NOT NULL DEFAULT 'offline'
        CHECK (status IN ('online','offline','error')),
    extension_version TEXT,
    current_job_id    INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    last_error        TEXT,
    last_heartbeat_at TEXT,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workers_heartbeat ON workers(last_heartbeat_at DESC);

-- ---------------------------------------------------------------------------
-- B9. SETTINGS -> Blocked pages (the one card of v1's Settings that survives).
--
-- 001 has pages.is_hidden, which is a boolean and cannot answer the three
-- columns the table prints: Reason, Blocked, By. It also has to hold pages
-- that were blocked before they were ever stored, so the key is the platform
-- page id, not pages.id.
--
-- `blocked_by` is deliberately absent: there is nobody to record.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS blocked_pages (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_page_id   TEXT    NOT NULL,
    page_id            INTEGER REFERENCES pages(id) ON DELETE SET NULL,
    page_name_snapshot TEXT    NOT NULL DEFAULT '',
    reason             TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL,
    UNIQUE (platform_page_id)
);
CREATE INDEX IF NOT EXISTS idx_blocked_pages_created ON blocked_pages(created_at DESC);

-- ---------------------------------------------------------------------------
-- B10. Settings -> Backups. v1 had no table: six .sqlite3 snapshots sat in a
-- folder and nothing knew about them. The card needs a list.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    filename   TEXT    NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    reason     TEXT    NOT NULL DEFAULT 'manual',    -- manual | pre-migration | scheduled
    created_at TEXT    NOT NULL,
    UNIQUE (filename)
);


-- ===========================================================================
-- C. VIEWS — the "no ALTER TABLE" tax, paid once.
--
-- Screens read these instead of joining the side tables by hand, so a missing
-- side-table row reads as the sensible default everywhere rather than as NULL
-- in one place and 0 in another.
-- ===========================================================================

-- A page plus its logical identity and its two extra flags. A page with no
-- page_identity row is its own logical page, which is the common case.
CREATE VIEW IF NOT EXISTS v_page AS
SELECT
    p.*,
    COALESCE(i.logical_key, 'page:' || p.platform_page_id) AS logical_key,
    COALESCE(i.display_name, p.alias, p.name)              AS logical_name,
    COALESCE(i.is_primary, 1)                              AS is_primary_source,
    COALESCE(s.is_saved, 0)                                AS is_saved,
    COALESCE(s.is_favorite, 0)                             AS is_favorite,
    COALESCE(s.is_removed, 0)                              AS is_removed
FROM pages p
LEFT JOIN page_identity i ON i.page_id = p.id
LEFT JOIN page_states   s ON s.page_id = p.id;

-- A product plus its four flags and its identity status.
CREATE VIEW IF NOT EXISTS v_product AS
SELECT
    pr.*,
    COALESCE(st.tracked, 0)              AS is_tracked,
    COALESCE(st.saved, 0)                AS is_saved,
    COALESCE(st.favorite, 0)             AS is_favorite,
    COALESCE(st.hidden, 0)               AS is_hidden,
    COALESCE(m.identity_status, 'unknown') AS identity_status,
    m.category                           AS category,
    m.product_type                       AS product_type,
    m.canonical_url                      AS canonical_url,
    m.store_platform                     AS store_platform
FROM products pr
LEFT JOIN product_states st ON st.product_id = pr.id
LEFT JOIN product_meta   m  ON m.product_id  = pr.id;

-- A group plus the fields the detail header and Edit modal use.
CREATE VIEW IF NOT EXISTS v_group AS
SELECT
    g.*,
    m.primary_domain AS primary_domain,
    m.category       AS category,
    m.color_key      AS color_key
FROM "groups" g
LEFT JOIN group_meta m ON m.group_id = g.id;

-- The newest FB-results reading per page, with its age in days — the Brand
-- Groups "FB results" column and its stale / very-stale colouring.
CREATE VIEW IF NOT EXISTS v_page_fb_results AS
SELECT
    h.page_id                                             AS page_id,
    h.estimated_results                                   AS estimated_results,
    h.captured_at                                         AS captured_at,
    CAST(julianday('now') - julianday(h.captured_at) AS INTEGER) AS age_days
FROM page_scan_history h
WHERE h.estimated_results IS NOT NULL
  AND h.id = (
      SELECT h2.id FROM page_scan_history h2
      WHERE h2.page_id = h.page_id AND h2.estimated_results IS NOT NULL
      ORDER BY h2.captured_at DESC, h2.id DESC LIMIT 1
  );
