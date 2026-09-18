"""Every read query the dashboard makes. One file, live SQL, no cache tables.

Why this file exists at all: in v1 the read queries were scattered across
``tabs/*/queries.py`` (one of them 4,773 lines) and half of them read from
denormalised cache tables that went stale. v2 has 17k ads — SQLite counts them
in single-digit milliseconds — so every number on every screen is counted from
``ads`` at request time and there is nothing to invalidate.

Two conventions the routes rely on:

* Everything returns plain ``dict``s (not ``sqlite3.Row``), already carrying the
  derived fields templates need (``delta``, ``staleness``, ``days_running``,
  parsed ``media_urls``...). Templates do presentation, never arithmetic.
* Sort keys and directions are looked up in whitelists. No caller-supplied text
  is ever interpolated into SQL.

The two numbers that matter most on the home screen:

    delta      = live active ad count - pages.prev_active_ads   (NULL if the
                 page has never completed a scan — "never scanned" is not "0")
    staleness  = whole days since pages.last_verified_at, bucketed
                 fresh (<=7d) / warn (<=30d) / stale (>30d) / never
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

from . import db
from .meta_links import meta_ad_url, meta_ads_library_url

# --- staleness thresholds (docs/02-PRD.md P0.1: ">7d / >30d" colouring) ------
STALE_WARN_DAYS = 7
STALE_BAD_DAYS = 30

# Job states that mean "this page is already spoken for" — used both for the
# queued badge and for the duplicate-job guard (PRD P0.2).
OPEN_JOB_STATES = ("pending", "claimed", "running")

# Outcomes that count as a real, finished scan of a page (docs/03 §3.5).
GOOD_OUTCOMES = ("complete", "exhausted", "empty")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _rows(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in db.fetch_all(sql, params)]


def _row(sql: str, params: Iterable[Any] = ()) -> dict | None:
    found = db.fetch_one(sql, params)
    return dict(found) if found is not None else None


def _placeholders(values: Sequence[Any]) -> str:
    return ",".join("?" for _ in values)


def parse_media_urls(raw: Any) -> list[str]:
    """``ads.media_urls`` is a JSON array of strings. Never trust it blindly —
    v1 data has a few rows that are a bare string or malformed JSON."""
    if isinstance(raw, list):
        return [str(u) for u in raw if u]
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return [text] if text.startswith("http") else []
    if isinstance(parsed, str):
        return [parsed] if parsed else []
    if isinstance(parsed, list):
        return [str(u) for u in parsed if u]
    return []


def staleness_bucket(days_since_scan: int | None) -> str:
    """'never' | 'fresh' | 'warn' | 'stale' — the pages-list colour class."""
    if days_since_scan is None:
        return "never"
    if days_since_scan > STALE_BAD_DAYS:
        return "stale"
    if days_since_scan > STALE_WARN_DAYS:
        return "warn"
    return "fresh"


def sparkline_points(
    values: Sequence[float | int],
    width: int = 120,
    height: int = 28,
) -> str:
    """SVG polyline ``points`` string for the page-detail sparkline.

    The arithmetic lives here rather than in the template so the template stays
    a template. Flat series draw a centred straight line instead of dividing by
    a zero range.
    """
    numbers = [float(v or 0) for v in values]
    if not numbers:
        return ""
    if len(numbers) == 1:
        numbers = numbers * 2
    low, high = min(numbers), max(numbers)
    span = high - low
    step = width / (len(numbers) - 1)
    pad = 2.0
    usable = height - (2 * pad)
    parts = []
    for index, value in enumerate(numbers):
        x = index * step
        y = (height / 2) if span == 0 else (pad + usable * (1 - (value - low) / span))
        parts.append(f"{x:.1f},{y:.1f}")
    return " ".join(parts)


def get_setting(key: str, default: str = "") -> str:
    row = db.fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
    return str(row["value"]) if row is not None else default


# ---------------------------------------------------------------------------
# pages list
# ---------------------------------------------------------------------------
# Counted live from `ads` on every request — pages.active_ads is ingest's
# bookkeeping, this is the truth. They should agree; if they ever don't, the
# screen shows reality rather than a stale counter.
_PAGE_AD_ROLLUP = """
    LEFT JOIN (
        SELECT page_id,
               SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END)                    AS live_active,
               COUNT(*)                                                              AS live_total,
               SUM(CASE WHEN status = 'active' THEN represented_ad_count ELSE 0 END) AS live_represented
        FROM ads
        GROUP BY page_id
    ) r ON r.page_id = p.id
"""

_PAGE_SELECT = f"""
SELECT
    p.id,
    p.platform_page_id,
    p.name,
    p.alias,
    p.url,
    p.profile_image_url,
    p.is_tracked,
    p.is_hidden,
    p.fb_estimated_results,
    p.current_scan_status,
    p.prev_active_ads,
    p.last_new_ads,
    p.last_stopped_ads,
    p.last_delta,
    p.last_verified_at,
    p.last_captured_at,
    p.first_captured_at,
    COALESCE(r.live_active, 0)      AS active_ads,
    COALESCE(r.live_total, 0)       AS total_ads,
    COALESCE(r.live_represented, 0) AS represented_ads,
    CASE
        WHEN p.last_verified_at IS NULL THEN NULL
        ELSE COALESCE(r.live_active, 0) - p.prev_active_ads
    END AS delta,
    CASE
        WHEN p.last_verified_at IS NULL THEN NULL
        ELSE CAST(julianday('now') - julianday(p.last_verified_at) AS INTEGER)
    END AS days_since_scan,
    (
        SELECT COUNT(*)
        FROM job_targets t
        JOIN jobs j ON j.id = t.job_id
        WHERE t.page_id = p.id
          AND j.status IN ('pending', 'claimed', 'running')
          AND t.status IN ('pending', 'running')
    ) AS open_targets
FROM pages p
{_PAGE_AD_ROLLUP}
"""

# key -> (ASC clause, DESC clause). Whitelist: nothing from the querystring is
# ever concatenated into SQL. NULL deltas (never scanned) always sink to the
# bottom, whichever direction is asked for — "unknown" is not "smallest".
PAGE_SORTS: dict[str, tuple[str, str]] = {
    "delta": (
        "(delta IS NULL), delta ASC, active_ads DESC",
        "(delta IS NULL), delta DESC, active_ads DESC",
    ),
    "active": ("active_ads ASC, p.name ASC", "active_ads DESC, p.name ASC"),
    "estimate": (
        "(p.fb_estimated_results IS NULL), p.fb_estimated_results ASC",
        "(p.fb_estimated_results IS NULL), p.fb_estimated_results DESC",
    ),
    "new": ("p.last_new_ads ASC, p.name ASC", "p.last_new_ads DESC, p.name ASC"),
    "stopped": (
        "p.last_stopped_ads ASC, p.name ASC",
        "p.last_stopped_ads DESC, p.name ASC",
    ),
    "scanned": (
        "(days_since_scan IS NULL), days_since_scan ASC",
        "(days_since_scan IS NULL) DESC, days_since_scan DESC",
    ),
    "name": (
        "COALESCE(NULLIF(p.alias, ''), p.name) COLLATE NOCASE ASC",
        "COALESCE(NULLIF(p.alias, ''), p.name) COLLATE NOCASE DESC",
    ),
}
DEFAULT_PAGE_SORT = "delta"


def page_order_by(sort: str | None, direction: str | None) -> tuple[str, str, str]:
    """Resolve (clause, sort_key, direction) from untrusted querystring values."""
    key = sort if sort in PAGE_SORTS else DEFAULT_PAGE_SORT
    want_desc = str(direction or "desc").lower() != "asc"
    ascending, descending = PAGE_SORTS[key]
    return (descending if want_desc else ascending), key, ("desc" if want_desc else "asc")


def _decorate_page(row: dict) -> dict:
    row["display_name"] = (row.get("alias") or "").strip() or (
        (row.get("name") or "").strip() or f"Page {row.get('platform_page_id') or row['id']}"
    )
    row["staleness"] = staleness_bucket(row.get("days_since_scan"))
    row["library_url"] = meta_ads_library_url(row.get("platform_page_id"), row.get("url"))
    row["is_queued"] = bool(row.get("open_targets") or 0) or row.get(
        "current_scan_status"
    ) in ("queued", "running")
    delta = row.get("delta")
    row["delta_direction"] = (
        "none" if delta is None else ("up" if delta > 0 else ("down" if delta < 0 else "flat"))
    )
    return row


def list_pages(
    *,
    search: str = "",
    sort: str | None = None,
    direction: str | None = None,
    include_hidden: bool = False,
    changed_only: bool = False,
    limit: int = 500,
) -> list[dict]:
    where = ["1 = 1"]
    params: list[Any] = []

    if not include_hidden:
        where.append("p.is_hidden = 0")

    needle = str(search or "").strip()
    if needle:
        where.append(
            "(p.name LIKE ? OR COALESCE(p.alias,'') LIKE ? OR p.platform_page_id LIKE ?)"
        )
        like = f"%{needle}%"
        params += [like, like, like]

    clause, _key, _dir = page_order_by(sort, direction)
    sql = f"{_PAGE_SELECT} WHERE {' AND '.join(where)} ORDER BY {clause} LIMIT ?"
    params.append(int(limit))

    rows = [_decorate_page(r) for r in _rows(sql, params)]
    if changed_only:
        rows = [r for r in rows if (r.get("delta") or 0) != 0 or (r.get("last_new_ads") or 0)]
    return rows


def get_page(page_id: int) -> dict | None:
    row = _row(f"{_PAGE_SELECT} WHERE p.id = ?", (int(page_id),))
    return _decorate_page(row) if row else None


def get_page_by_platform_id(platform_page_id: str) -> dict | None:
    row = _row(f"{_PAGE_SELECT} WHERE p.platform_page_id = ?", (str(platform_page_id),))
    return _decorate_page(row) if row else None


def pages_overview() -> dict:
    """The four numbers above the pages table."""
    row = _row(
        f"""
        SELECT
            COUNT(*)                                          AS pages,
            SUM(CASE WHEN staleness_days IS NULL
                      OR staleness_days > {STALE_BAD_DAYS} THEN 1 ELSE 0 END) AS stale_pages,
            SUM(CASE WHEN delta > 0 THEN 1 ELSE 0 END)        AS pages_up,
            SUM(CASE WHEN delta < 0 THEN 1 ELSE 0 END)        AS pages_down,
            SUM(active_ads)                                   AS active_ads,
            SUM(COALESCE(last_new_ads, 0))                    AS new_ads
        FROM (
            SELECT
                COALESCE(r.live_active, 0) AS active_ads,
                p.last_new_ads             AS last_new_ads,
                CASE WHEN p.last_verified_at IS NULL THEN NULL
                     ELSE COALESCE(r.live_active, 0) - p.prev_active_ads END AS delta,
                CASE WHEN p.last_verified_at IS NULL THEN NULL
                     ELSE CAST(julianday('now') - julianday(p.last_verified_at) AS INTEGER)
                END AS staleness_days
            FROM pages p
            {_PAGE_AD_ROLLUP}
            WHERE p.is_hidden = 0
        )
        """
    )
    return {k: int(v or 0) for k, v in (row or {}).items()}


# ---------------------------------------------------------------------------
# "since the last scan" — the feature the owner actually uses
# ---------------------------------------------------------------------------
def last_scan_window(page_id: int) -> dict:
    """When did the most recent completed scan of this page start?

    Primary source is ``job_targets`` (the real scan record). Pages imported
    from v1, or scanned before job_targets existed, fall back to
    ``pages.last_verified_at``; ``source`` says which, so the template can be
    honest about it.
    """
    target = _row(
        f"""
        SELECT t.id, t.job_id, t.started_at, t.finished_at, t.outcome,
               t.unique_ads, t.scrolls
        FROM job_targets t
        WHERE t.page_id = ?
          AND t.status = 'done'
          AND t.outcome IN ({_placeholders(GOOD_OUTCOMES)})
          AND t.started_at IS NOT NULL
        ORDER BY t.started_at DESC, t.id DESC
        LIMIT 1
        """,
        (int(page_id), *GOOD_OUTCOMES),
    )
    if target:
        return {
            "started_at": target["started_at"],
            "finished_at": target["finished_at"],
            "job_id": target["job_id"],
            "outcome": target["outcome"],
            "source": "job_target",
        }

    page = _row(
        "SELECT last_verified_at, last_captured_at FROM pages WHERE id = ?",
        (int(page_id),),
    )
    verified = (page or {}).get("last_verified_at")
    return {
        "started_at": verified,
        "finished_at": verified,
        "job_id": None,
        "outcome": None,
        "source": "page" if verified else "none",
    }


def page_new_ads(page_id: int, limit: int = 200) -> list[dict]:
    """Ads first captured during (or after) the last completed scan."""
    window = last_scan_window(page_id)
    if not window["started_at"]:
        return []
    return [
        _decorate_ad(r)
        for r in _rows(
            f"""
            {_AD_SELECT}
            WHERE a.page_id = ? AND a.first_captured_at >= ?
            ORDER BY a.first_captured_at DESC, a.id DESC
            LIMIT ?
            """,
            (int(page_id), window["started_at"], int(limit)),
        )
    ]


def page_stopped_ads(page_id: int, limit: int = 200) -> list[dict]:
    """Ads reconciliation switched off during the last completed scan."""
    window = last_scan_window(page_id)
    if not window["started_at"]:
        return []
    return [
        _decorate_ad(r)
        for r in _rows(
            f"""
            {_AD_SELECT}
            WHERE a.page_id = ? AND a.status = 'inactive' AND a.updated_at >= ?
            ORDER BY a.updated_at DESC, a.id DESC
            LIMIT ?
            """,
            (int(page_id), window["started_at"], int(limit)),
        )
    ]


def page_changes(page_id: int, limit: int = 60) -> dict:
    """Everything the delta cell drills into: what started, what stopped."""
    page = get_page(page_id)
    window = last_scan_window(page_id)
    return {
        "page": page,
        "window": window,
        "new_ads": page_new_ads(page_id, limit),
        "stopped_ads": page_stopped_ads(page_id, limit),
    }


# ---------------------------------------------------------------------------
# ads
# ---------------------------------------------------------------------------
_AD_SELECT = """
SELECT
    a.id, a.page_id, a.library_id, a.status, a.start_date, a.end_date,
    a.ad_text, a.headline, a.description, a.cta, a.destination_url,
    a.media_type, a.media_urls, a.represented_ad_count, a.content_hash,
    a.first_captured_at, a.last_captured_at, a.updated_at,
    CASE
        WHEN a.start_date IS NULL OR a.start_date = '' THEN NULL
        ELSE CAST(julianday(COALESCE(NULLIF(a.end_date, ''), 'now'))
                  - julianday(a.start_date) AS INTEGER)
    END AS days_running,
    (SELECT COUNT(*) FROM ad_versions v WHERE v.ad_id = a.id) AS version_count
FROM ads a
"""

AD_SORTS: dict[str, str] = {
    "longest": "(days_running IS NULL), days_running DESC, a.id DESC",
    "newest": "(a.start_date IS NULL), a.start_date DESC, a.id DESC",
    "recent": "a.last_captured_at DESC, a.id DESC",
    "first_seen": "a.first_captured_at DESC, a.id DESC",
    "copies": "a.represented_ad_count DESC, a.id DESC",
}
DEFAULT_AD_SORT = "newest"


def _decorate_ad(row: dict) -> dict:
    media = parse_media_urls(row.get("media_urls"))
    row["media_list"] = media
    row["media_count"] = len(media)
    row["preview_url"] = media[0] if media else ""
    row["is_video"] = str(row.get("media_type") or "").lower() == "video"
    row["library_url"] = meta_ad_url(row.get("library_id"))
    text = str(row.get("ad_text") or "").strip()
    row["snippet"] = (text[:180] + "…") if len(text) > 180 else text
    return row


def page_ads(
    page_id: int,
    *,
    status: str = "active",
    search: str = "",
    sort: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    where = ["a.page_id = ?"]
    params: list[Any] = [int(page_id)]

    if status in ("active", "inactive"):
        where.append("a.status = ?")
        params.append(status)

    needle = str(search or "").strip()
    if needle:
        where.append(
            "(COALESCE(a.ad_text,'') LIKE ? OR COALESCE(a.headline,'') LIKE ?"
            " OR COALESCE(a.destination_url,'') LIKE ? OR a.library_id LIKE ?)"
        )
        like = f"%{needle}%"
        params += [like, like, like, like]

    order = AD_SORTS.get(sort or DEFAULT_AD_SORT, AD_SORTS[DEFAULT_AD_SORT])
    params += [int(limit), int(offset)]
    sql = f"{_AD_SELECT} WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ? OFFSET ?"
    return [_decorate_ad(r) for r in _rows(sql, params)]


def page_ad_counts(page_id: int) -> dict:
    row = _row(
        """
        SELECT
            COUNT(*)                                                   AS total,
            SUM(CASE WHEN status = 'active'   THEN 1 ELSE 0 END)       AS active,
            SUM(CASE WHEN status = 'inactive' THEN 1 ELSE 0 END)       AS inactive,
            SUM(CASE WHEN status = 'active'
                     THEN represented_ad_count ELSE 0 END)             AS represented
        FROM ads WHERE page_id = ?
        """,
        (int(page_id),),
    )
    return {k: int(v or 0) for k, v in (row or {}).items()}


def get_ad(ad_id: int) -> dict | None:
    row = _row(f"{_AD_SELECT} WHERE a.id = ?", (int(ad_id),))
    if not row:
        return None
    ad = _decorate_ad(row)
    page = _row(
        "SELECT id, name, alias, platform_page_id, url FROM pages WHERE id = ?",
        (ad["page_id"],),
    )
    if page:
        page["display_name"] = (page.get("alias") or "").strip() or page.get("name") or ""
    ad["page"] = page
    return ad


def get_ad_by_library_id(library_id: str) -> dict | None:
    row = _row(f"{_AD_SELECT} WHERE a.library_id = ?", (str(library_id),))
    return get_ad(int(row["id"])) if row else None


def ad_versions(ad_id: int) -> list[dict]:
    rows = _rows(
        """
        SELECT id, ad_id, version_number, content_hash, captured_at, ad_text,
               headline, description, cta, destination_url, media_urls,
               change_summary
        FROM ad_versions
        WHERE ad_id = ?
        ORDER BY version_number DESC
        """,
        (int(ad_id),),
    )
    for row in rows:
        row["media_list"] = parse_media_urls(row.get("media_urls"))
    return rows


def ad_products(ad_id: int) -> list[dict]:
    return _rows(
        """
        SELECT pr.id, pr.display_name, pr.normalized_name, pr.product_url,
               pr.domain, pr.shortlist_state, ap.method, ap.confidence
        FROM ad_products ap
        JOIN products pr ON pr.id = ap.product_id
        WHERE ap.ad_id = ?
        ORDER BY pr.display_name
        """,
        (int(ad_id),),
    )


# ---------------------------------------------------------------------------
# page history / sparkline
# ---------------------------------------------------------------------------
def page_daily_metrics(page_id: int, days: int = 14) -> list[dict]:
    rows = _rows(
        """
        SELECT metric_date, active_ads, new_ads, stopped_ads, represented_ads
        FROM page_daily_metrics
        WHERE page_id = ?
        ORDER BY metric_date DESC
        LIMIT ?
        """,
        (int(page_id), int(days)),
    )
    rows.reverse()  # oldest -> newest, the direction a sparkline is drawn in
    return rows


def page_scan_history(page_id: int, limit: int = 12) -> list[dict]:
    return _rows(
        """
        SELECT t.id, t.job_id, t.position, t.status, t.outcome, t.scrolls,
               t.unique_ads, t.represented_ads, t.estimated_results, t.message,
               t.started_at, t.finished_at,
               j.status AS job_status, j.job_type, j.created_at AS job_created_at
        FROM job_targets t
        JOIN jobs j ON j.id = t.job_id
        WHERE t.page_id = ?
        ORDER BY t.id DESC
        LIMIT ?
        """,
        (int(page_id), int(limit)),
    )


# ---------------------------------------------------------------------------
# queue
# ---------------------------------------------------------------------------
_JOB_SELECT = """
SELECT
    j.id, j.job_type, j.status, j.label, j.outcome, j.error, j.error_code,
    j.retryable, j.retry_count, j.max_retries, j.targets_total, j.targets_done,
    j.lease_expires_at, j.claimed_at, j.started_at, j.finished_at,
    j.cancel_requested_at, j.created_at, j.updated_at,
    (SELECT COUNT(*) FROM job_targets t WHERE t.job_id = j.id) AS targets,
    (SELECT COUNT(*) FROM job_targets t WHERE t.job_id = j.id AND t.status = 'done')    AS done_targets,
    (SELECT COUNT(*) FROM job_targets t WHERE t.job_id = j.id AND t.status = 'failed')  AS failed_targets,
    (SELECT COUNT(*) FROM job_targets t WHERE t.job_id = j.id AND t.status = 'running') AS running_targets,
    (SELECT COALESCE(SUM(t.unique_ads), 0) FROM job_targets t WHERE t.job_id = j.id)    AS unique_ads,
    (SELECT COALESCE(SUM(t.scrolls), 0)    FROM job_targets t WHERE t.job_id = j.id)    AS scrolls,
    (SELECT COUNT(*) FROM job_batches b WHERE b.job_id = j.id)                          AS batches,
    (SELECT COALESCE(SUM(t.estimated_results), 0) FROM job_targets t WHERE t.job_id = j.id) AS est_total
FROM jobs j
"""

JOB_OPEN_STATES = OPEN_JOB_STATES


def _coverage_class(pct: float | None) -> str:
    """CSS class for a coverage percentage: <90 red, <95 amber, else none.

    Takes the UNROUNDED value: 94.6% is below 95 and must colour amber even
    though the displayed whole-percent rounds to 95.
    """
    if pct is None:
        return ""
    if pct < 90:
        return "cov-red"
    if pct < 95:
        return "cov-amber"
    return ""


def _coverage_pct(seen: int, estimate: int) -> tuple[int | None, str]:
    raw = (100.0 * seen / estimate) if estimate else None
    pct = min(100, int(round(raw))) if raw is not None else None
    return pct, _coverage_class(raw)


def _decorate_job(row: dict) -> dict:
    total = int(row.get("targets") or 0)
    done = int(row.get("done_targets") or 0) + int(row.get("failed_targets") or 0)
    row["progress_pct"] = int(round(100 * done / total)) if total else 0
    row["is_open"] = row.get("status") in OPEN_JOB_STATES
    row["can_retry"] = row.get("status") in ("failed", "cancelled") or (
        row.get("status") == "completed" and int(row.get("failed_targets") or 0) > 0
    )
    row["can_cancel"] = row["is_open"]
    row["can_delete"] = row.get("status") in (
        "pending", "completed", "failed", "cancelled",
    )
    est = int(row.get("est_total") or 0)
    seen = int(row.get("unique_ads") or 0)
    row["coverage_pct"], row["coverage_class"] = _coverage_pct(seen, est)
    return row


def list_jobs(*, status: str = "", limit: int = 40) -> list[dict]:
    where = ""
    params: list[Any] = []
    if status == "open":
        where = f"WHERE j.status IN ({_placeholders(OPEN_JOB_STATES)})"
        params += list(OPEN_JOB_STATES)
    elif status in ("pending", "claimed", "running", "completed", "failed", "cancelled"):
        where = "WHERE j.status = ?"
        params.append(status)
    params.append(int(limit))
    return [
        _decorate_job(r)
        for r in _rows(f"{_JOB_SELECT} {where} ORDER BY j.id DESC LIMIT ?", params)
    ]


def get_job(job_id: int) -> dict | None:
    row = _row(f"{_JOB_SELECT} WHERE j.id = ?", (int(job_id),))
    return _decorate_job(row) if row else None


def job_targets(job_id: int) -> list[dict]:
    rows = _rows(
        """
        SELECT t.id, t.job_id, t.position, t.page_id, t.platform_page_id,
               t.page_url, t.label, t.status, t.outcome, t.scrolls, t.unique_ads,
               t.represented_ads, t.estimated_results, t.message,
               t.started_at, t.finished_at,
               p.name AS page_name, p.alias AS page_alias,
               COALESCE(p.active_ads, 0) AS page_live_ads
        FROM job_targets t
        LEFT JOIN pages p ON p.id = t.page_id
        WHERE t.job_id = ?
        ORDER BY t.position ASC, t.id ASC
        """,
        (int(job_id),),
    )
    for row in rows:
        row["display_name"] = (
            (row.get("page_alias") or "").strip()
            or (row.get("page_name") or "").strip()
            or (row.get("label") or "").strip()
            or f"target {row.get('position')}"
        )
        estimate = int(row.get("estimated_results") or 0)
        seen = int(row.get("unique_ads") or 0)
        row["coverage_pct"], row["coverage_class"] = _coverage_pct(seen, estimate)
    return rows


def job_batches(job_id: int, limit: int = 25) -> list[dict]:
    return _rows(
        """
        SELECT id, batch_id, batch_sequence, target_position, page_id, is_final,
               outcome, status, ads_seen, ads_new, ads_updated, ads_deactivated,
               received_at
        FROM job_batches
        WHERE job_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(job_id), int(limit)),
    )


def _accepted_library_ids(*, job_id: int | None = None,
                         exclude_job_id: int | None = None) -> set[str]:
    """library_ids accepted by ``accepted`` batches.

    Pass ``job_id`` for one job's union, or ``exclude_job_id`` for every other
    job's union — the pair drives job-delete's exclusive-ad computation.
    """
    sql = "SELECT accepted_ad_ids_json FROM job_batches WHERE status = 'accepted'"
    params: list[Any] = []
    if job_id is not None:
        sql += " AND job_id = ?"
        params.append(int(job_id))
    if exclude_job_id is not None:
        sql += " AND job_id != ?"
        params.append(int(exclude_job_id))
    ids: set[str] = set()
    for row in db.fetch_all(sql, params):
        raw = row[0] if not isinstance(row, dict) else row.get("accepted_ad_ids_json")
        try:
            parsed = json.loads(raw or "[]")
        except (TypeError, ValueError):
            continue
        for value in parsed if isinstance(parsed, list) else []:
            text = str(value or "").strip()
            if text:
                ids.add(text)
    return ids


def job_delete_impact(job_id: int) -> dict | None:
    """What deleting a job would remove — shown on the confirm screen.

    ``exclusive_ads`` counts ads whose library_id was accepted by this job's
    batches and by no other job's: those are the only ad rows a delete takes
    with it. Ads re-captured by a later scan stay, because they are that
    scan's data too.
    """
    job = get_job(job_id)
    if job is None:
        return None
    mine = _accepted_library_ids(job_id=int(job_id))
    others = _accepted_library_ids(exclude_job_id=int(job_id))
    exclusive = mine - others
    exclusive_count = 0
    if exclusive:
        row = db.fetch_one(
            f"SELECT COUNT(*) FROM ads WHERE library_id IN "
            f"({_placeholders(list(exclusive))})",
            list(exclusive),
        )
        exclusive_count = int(row[0] if row else 0)
    targets = db.fetch_one(
        "SELECT COUNT(*) FROM job_targets WHERE job_id = ?", (int(job_id),)
    )
    batches = db.fetch_one(
        "SELECT COUNT(*) FROM job_batches WHERE job_id = ?", (int(job_id),)
    )
    return {
        "job": job,
        "targets": int(targets[0] if targets else 0),
        "batches": int(batches[0] if batches else 0),
        "exclusive_ads": exclusive_count,
    }


def queue_counts() -> dict:
    row = _row(
        """
        SELECT
            SUM(CASE WHEN status = 'pending'              THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN status IN ('claimed','running') THEN 1 ELSE 0 END) AS running,
            SUM(CASE WHEN status = 'failed'               THEN 1 ELSE 0 END) AS failed,
            SUM(CASE WHEN status = 'completed'            THEN 1 ELSE 0 END) AS completed,
            COUNT(*)                                                         AS total
        FROM jobs
        """
    )
    return {k: int(v or 0) for k, v in (row or {}).items()}


def pages_with_open_targets(page_ids: Sequence[int]) -> set[int]:
    """Which of these pages are already queued or running? (PRD P0.2: never
    create a second pending job for the same page.)"""
    ids = [int(p) for p in page_ids]
    if not ids:
        return set()
    rows = db.fetch_all(
        f"""
        SELECT DISTINCT t.page_id
        FROM job_targets t
        JOIN jobs j ON j.id = t.job_id
        WHERE t.page_id IN ({_placeholders(ids)})
          AND j.status IN ({_placeholders(OPEN_JOB_STATES)})
          AND t.status IN ('pending', 'running')
        """,
        (*ids, *OPEN_JOB_STATES),
    )
    return {int(r[0]) for r in rows}


def pages_for_targets(page_ids: Sequence[int]) -> list[dict]:
    """Page rows in the order given, for building job targets."""
    ids = [int(p) for p in page_ids]
    if not ids:
        return []
    rows = _rows(
        f"""
        SELECT id, platform_page_id, name, alias, url
        FROM pages
        WHERE id IN ({_placeholders(ids)})
        """,
        ids,
    )
    by_id = {int(r["id"]): r for r in rows}
    ordered = []
    for page_id in ids:
        row = by_id.get(page_id)
        if row is None:
            continue
        row["display_name"] = (row.get("alias") or "").strip() or row.get("name") or ""
        row["library_url"] = meta_ads_library_url(row.get("platform_page_id"), row.get("url"))
        ordered.append(row)
    return ordered


__all__ = [
    "STALE_WARN_DAYS",
    "STALE_BAD_DAYS",
    "staleness_bucket",
    "sparkline_points",
    "parse_media_urls",
    "get_setting",
    "PAGE_SORTS",
    "page_order_by",
    "list_pages",
    "get_page",
    "get_page_by_platform_id",
    "pages_overview",
    "last_scan_window",
    "page_new_ads",
    "page_stopped_ads",
    "page_changes",
    "page_ads",
    "page_ad_counts",
    "get_ad",
    "get_ad_by_library_id",
    "ad_versions",
    "ad_products",
    "page_daily_metrics",
    "page_scan_history",
    "list_jobs",
    "get_job",
    "job_targets",
    "job_batches",
    "queue_counts",
    "pages_with_open_targets",
    "pages_for_targets",
]
