-- 005_transcription.sql
--
-- Transcription is the one thing in this product that SPENDS MONEY, so it never
-- runs on its own: no fetch hook, no ingest hook, no scheduler, no startup
-- backfill. It runs when the owner presses "Generate scripts", and this table is
-- the receipt for each press.
--
-- Everything transcription READS and WRITES already exists (migration 002):
--   transcripts     one row per unique creative, keyed by media_url_hash
--   transcript_ads  which ads share that creative (dedupe fan-out)
--   script_clusters "50 ads are really 5 videos" — the tool's whole point
--   ad_languages    per-ad language, text + media reconciled
-- What was missing is the RUN: who asked, for which product, in which
-- languages, how far it got, what it cost in provider calls, and why it stopped.
-- Without it a half-finished run is indistinguishable from one that never
-- started, and the owner's complaint #6 (LOGS) applies to spend as much as to
-- bugs.
--
-- Additive only, and re-runnable: the migration runner replays a file whose
-- bookkeeping INSERT did not land, so every statement here is IF NOT EXISTS.
-- No ALTER TABLE — 002's tables are carrying 20,183 imported ads.

CREATE TABLE IF NOT EXISTS transcription_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id       INTEGER REFERENCES products(id) ON DELETE CASCADE,
    status           TEXT    NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','completed','failed','cancelled')),
    -- Comma-separated language codes the owner asked for, '' = every language.
    -- 'unknown' means ads with no detected language yet.
    languages        TEXT    NOT NULL DEFAULT '',
    -- The provider the run RESOLVED to, per language routing, recorded after
    -- the fact: 'sarvam' for Indic when a Sarvam key exists, else 'groq'.
    -- NEVER a key, never a fragment of one.
    provider         TEXT    NOT NULL DEFAULT '',
    total            INTEGER NOT NULL DEFAULT 0,   -- rows this run must process
    processed        INTEGER NOT NULL DEFAULT 0,   -- rows finished, any outcome
    completed        INTEGER NOT NULL DEFAULT 0,   -- real transcripts written
    deduped          INTEGER NOT NULL DEFAULT 0,   -- content-hash copies, 0 API calls
    failed           INTEGER NOT NULL DEFAULT 0,
    skipped          INTEGER NOT NULL DEFAULT 0,   -- no media / wrong language
    api_calls        INTEGER NOT NULL DEFAULT 0,   -- what this run actually cost
    clusters_created INTEGER NOT NULL DEFAULT 0,
    message          TEXT,
    error            TEXT,
    created_at       TEXT    NOT NULL,
    started_at       TEXT,
    heartbeat_at     TEXT,
    finished_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_transcription_runs_product
    ON transcription_runs(product_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_transcription_runs_status
    ON transcription_runs(status, id DESC);
