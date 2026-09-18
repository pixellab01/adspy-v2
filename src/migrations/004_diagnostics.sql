-- ===========================================================================
-- 004_diagnostics.sql — the LOGS screen's durable memory.
--
-- The owner's request, verbatim in substance: "if any bug or anything comes
-- up, one click and it is saved to the logs, and whenever we restart, the logs
-- tell me which bugs happened and what still needs solving."
--
-- So this is not a text dump. It is an ISSUE LOG: one row per distinct
-- problem, counted when it recurs, closed by hand when it is fixed, and still
-- there after a restart. gunicorn's logs/server.*.log are a tail that rotates
-- into oblivion and is unreadable from the UI; these three tables are what the
-- screen actually reads.
--
-- ADDITIVE ONLY. 001/002/003 hold the owner's 20,000 ads. Nothing here ALTERs
-- or DROPs a table those migrations created, and every statement is
-- IF NOT EXISTS so a replay is harmless (app/db.py::run_migrations only
-- records the bookkeeping row *after* the script runs, so a crash mid-file
-- means the whole file is replayed next boot).
--
-- Deliberately NO foreign keys to jobs(id) / pages(id). An issue outlives the
-- thing it happened to: `DELETE FROM jobs` must never quietly erase the record
-- that a job failed. job_id / page_id are plain integers, joined optimistically
-- by the screen and rendered as bare numbers when the row is gone.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 1. issue_log — one row per distinct problem, not per occurrence.
--
-- `fingerprint` is the whole anti-nag mechanism (the same idea alerts.dedupe_key
-- uses): kind + where + the message with every digit collapsed to '#'. The same
-- failure on the same page twenty times is ONE row with occurrences=20, so a
-- broken page cannot bury the other nineteen problems.
--
-- Recurrence after a Resolve reopens the row (resolved_at back to NULL,
-- reopened_count += 1). "I fixed it" is a claim the next occurrence disproves.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS issue_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint    TEXT    NOT NULL,                 -- sha1(kind|location|normalised message)
    kind           TEXT    NOT NULL,                 -- server_error | scan_failed | scan_partial |
                                                     -- job_failed | batch_rejected | page_error |
                                                     -- worker_block | worker_error | manual
    severity       TEXT    NOT NULL DEFAULT 'warn'
        CHECK (severity IN ('info','warn','error')),
    source         TEXT    NOT NULL DEFAULT 'server'
        CHECK (source IN ('server','extension','manual')),
    title          TEXT    NOT NULL,                 -- WHAT: one line, human first
    detail         TEXT    NOT NULL DEFAULT '',      -- traceback tail / outcome / raw message
    location       TEXT    NOT NULL DEFAULT '',      -- WHERE: route, or "job 4 - target 10 - Naaptol-3"
    entity_type    TEXT,                             -- page | job | target | batch | worker
    entity_id      INTEGER,
    job_id         INTEGER,                          -- no FK, on purpose (see header)
    page_id        INTEGER,                          -- no FK, on purpose (see header)
    occurrences    INTEGER NOT NULL DEFAULT 1,       -- HOW MANY TIMES it recurred
    reopened_count INTEGER NOT NULL DEFAULT 0,       -- how often it came back after a Resolve
    first_seen_at  TEXT    NOT NULL,
    last_seen_at   TEXT    NOT NULL,
    resolved_at    TEXT,                             -- NULL = still open = still nagging
    resolved_note  TEXT    NOT NULL DEFAULT '',
    UNIQUE (fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_issue_log_open ON issue_log(resolved_at, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_issue_log_kind ON issue_log(kind, last_seen_at DESC);

-- ---------------------------------------------------------------------------
-- 2. worker_log_lines — the extension's 200-line ring buffer, landed server-side.
--
-- extension/lib/log.js keeps its log in chrome.storage.local and the panel has
-- a Clear button and no export, so today the dashboard has no idea what the
-- worker saw. POST /api/logs/worker ships the whole buffer; the extension
-- re-sends lines it has already sent, which is why (worker_id, line_hash) is
-- UNIQUE and the insert is INSERT OR IGNORE — replay is free.
--
-- Bounded to WORKER_LINE_LIMIT rows by app/log_service.py on every write.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS worker_log_lines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id   TEXT    NOT NULL DEFAULT '',
    level       TEXT    NOT NULL DEFAULT 'info',
    logged_at   TEXT    NOT NULL DEFAULT '',         -- the extension's own clock, verbatim
    message     TEXT    NOT NULL DEFAULT '',
    line_hash   TEXT    NOT NULL,                    -- sha1(logged_at|level|message)
    received_at TEXT    NOT NULL,
    UNIQUE (worker_id, line_hash)
);
CREATE INDEX IF NOT EXISTS idx_worker_log_recent ON worker_log_lines(id DESC);

-- ---------------------------------------------------------------------------
-- 3. log_snapshots — one row per "Save current logs" click.
--
-- The click writes ONE file under logs/snapshots/ holding the open issues, the
-- recently resolved ones, the extension buffer and the tail of both gunicorn
-- logs. The row is the index: what was captured, when, how big, and how many
-- issues were open at that moment. Files outlive the process; that is the
-- entire point of the feature.
--
-- Bounded to SNAPSHOT_LIMIT files by app/log_service.py; the oldest file and
-- its row go together.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS log_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    filename     TEXT    NOT NULL,
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    open_issues  INTEGER NOT NULL DEFAULT 0,
    total_issues INTEGER NOT NULL DEFAULT 0,
    note         TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL,
    UNIQUE (filename)
);
CREATE INDEX IF NOT EXISTS idx_log_snapshots_created ON log_snapshots(created_at DESC);
