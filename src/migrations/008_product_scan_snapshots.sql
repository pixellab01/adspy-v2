-- 008_product_scan_snapshots.sql
--
-- Per-scan ad-volume history for products, the data behind "18 ads the, ab
-- 21 hain, kitni growth hui". One row per (product, page, job): after a
-- page_scan target reconciles, ingest writes how many of this product's ads
-- are live on that page, how many are new, and how many just stopped.
-- Aggregating over pages gives the product's scan history; diffing two
-- consecutive snapshots gives the per-scan growth %.
CREATE TABLE IF NOT EXISTS product_scan_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    page_id     INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    job_id      INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    scanned_at  TEXT    NOT NULL,   -- UTC timestamp of the completed scan
    active_ads  INTEGER NOT NULL DEFAULT 0,  -- this product's LIVE ads on this page
    new_ads     INTEGER NOT NULL DEFAULT 0,  -- first seen in this scan
    stopped_ads INTEGER NOT NULL DEFAULT 0,  -- marked inactive by this scan
    created_at  TEXT    NOT NULL,
    UNIQUE (product_id, page_id, job_id)
);
CREATE INDEX IF NOT EXISTS idx_pss_product_time
    ON product_scan_snapshots(product_id, scanned_at);
CREATE INDEX IF NOT EXISTS idx_pss_page_time
    ON product_scan_snapshots(page_id, scanned_at);
CREATE INDEX IF NOT EXISTS idx_pss_job
    ON product_scan_snapshots(job_id);
