-- 006_transcription_v2.sql
--
-- Transcription v2: the per-video table the owner asked for ("click a product,
-- transcribe its videos, tell me WHICH LANGUAGE each one is in") needs six
-- facts per creative that 001's `transcripts` never stored, plus a cancel flag
-- on runs. All of them are nullable, none has a default that changes existing
-- rows, and nothing here touches the 20k imported ads.
--
-- Columns, and why each exists:
--   duration_seconds   Groq returns it, Sarvam does not; ffmpeg fills the gap.
--                      The per-video row shows m:ss and validate_transcript's
--                      words-per-second guard reads it on a re-run.
--   provider_language  the RAW code the provider reported, BEFORE
--                      reconcile_transcript_language overruled it with script
--                      evidence. Shown as "provider said hi, script says mr" so
--                      the owner can see how the language was decided.
--   library_id         the Ad Library id the media URL came from. fbcdn links
--                      expire (100% of the stored ones already have), and the
--                      public /ads/library/?id=<library_id> page is where a
--                      fresh one is read from — app/media_refresh.py.
--   media_path_hash    sha256 of the URL's PATH only. The same ad's video keeps
--                      its path across re-scans while the fbcdn HOST rotates
--                      (1,081 of 1,084 multi-version ads), so host+path (the
--                      old media_url_hash) queued the same file again on every
--                      re-scan. New rows write the path hash into both columns;
--                      lookups match either, so imported rows still hit.
--   refreshed_at       when media_url was last re-resolved from the Ad Library.
--   thumb_path         local poster frame (data/media/thumbs/<sha>.jpg) that,
--                      unlike the fbcdn poster, never expires.
--   cancel_requested_at on transcription_runs: the Cancel button. process_run
--                      checks it before every creative and finishes 'cancelled';
--                      pending rows stay pending for the next press.
--
-- SQLite has no ADD COLUMN IF NOT EXISTS. The runner (app/db.py run_migrations)
-- applies a file once and records it, so each ALTER appears here exactly once.
-- Unlike 002/005 this file is NOT re-runnable statement by statement; that is
-- the price of adding columns to a live table without copying 622 paid-for
-- transcripts into a new one.

ALTER TABLE transcripts ADD COLUMN duration_seconds  REAL;
ALTER TABLE transcripts ADD COLUMN provider_language TEXT;
ALTER TABLE transcripts ADD COLUMN library_id        TEXT;
ALTER TABLE transcripts ADD COLUMN media_path_hash   TEXT;
ALTER TABLE transcripts ADD COLUMN refreshed_at      TEXT;
ALTER TABLE transcripts ADD COLUMN thumb_path        TEXT;

CREATE INDEX IF NOT EXISTS idx_transcripts_path_hash ON transcripts(media_path_hash);

ALTER TABLE transcription_runs ADD COLUMN cancel_requested_at TEXT;
