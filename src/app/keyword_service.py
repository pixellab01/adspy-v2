"""Keyword Research — saved searches, the research queue, runs, results, and
the TWO-STAGE flow (discover pages, then scan them).

v1 split this across ``tabs/keyword_research/{actions,queries,ui}.py`` (5,773
lines) and four tables. v2 has the three from ``migrations/002_parity.sql``:

    keyword_queries   a saved search   (keyword + the four filters)
    keyword_runs      one execution of that search
    keyword_results   the pages one run ranked (v1 parity, rebuilt per run)

plus, from ``migrations/007_keyword_discovery.sql``:

    keyword_discovered_pages   the cross-run REVIEW LIST: one row per
                               (saved search, page) with ads seen, times seen,
                               identity kind and a review status
    keyword_run_chain          per-run materialisation + stage-2 bookkeeping
    keyword_query_automation   the "auto-scan" toggle + its threshold

and *no fourth table for the queue*. v1's ``keyword_research_manual_queue`` is
just "a run that has not been dispatched yet", so here a queue item **is** a
``keyword_runs`` row with ``job_id IS NULL`` and status ``pending``. Pressing
Research on it creates the job; nothing is copied between two tables and there
is no way for the two to disagree.

THE TWO STAGES.

  STAGE 1 — DISCOVER (job_type ``keyword``). A keyword search rides on an
  ordinary ``jobs`` row of type ``keyword`` with exactly one ``job_targets``
  row whose ``page_id`` is NULL and whose ``page_url`` is the Ad Library
  keyword-search URL (filters applied, caps in the fragment — see
  :func:`keyword_search_url`). The extension scrolls it, harvests every ad WITH
  that ad's own advertiser page, and posts batches. ``app/ingest.py`` resolves
  each ad's page and stores the ads. This module then MATERIALISES: after every
  batch it rebuilds ``keyword_results`` for the run straight from
  ``job_batches`` and upserts ``keyword_discovered_pages`` for the saved search
  (:func:`rebuild_run_results`). Discovered pages are NOT tracked — pages a
  keyword run created are untracked here, so a discovery never leaks into the
  Pages screen as a tracked competitor until the owner accepts it.

  STAGE 2 — SCAN (job_type ``page_scan``). Accept -> Track -> Scan. The owner
  accepts discovered pages (or presses Scan all), and :func:`stage2_scan`
  marks them tracked and creates a normal page_scan job through
  ``app.routes.queue.create_scan_job`` -> ``job_service.create_job``, so the
  one-page-one-scan duplicate guard (PRD P0.2) and the "no numeric id, no scan"
  filter both still apply. Bulk mode: a saved search with ``auto_scan`` on
  fires stage 2 by itself the moment its stage-1 run completes
  (:func:`advance_chains`), for every discovered page with at least ``min_ads``
  matching ads. The trigger is worker traffic (``/claim`` and ``/done``, hooked
  from ``app/routes/keyword.py``), so it chains overnight with nobody watching.

The two rules this module must never weaken, both enforced downstream in
``app/ingest.py`` and asserted by ``tests/test_keyword_flow.py``:

  * a keyword batch never reconciles (``ingest.reconcile`` requires
    ``job_type == 'page_scan'``), so a keyword sweep can never mass-deactivate
    a page's ads;
  * an ad whose page cannot be resolved is skipped and counted, never turned
    into a placeholder page.

``app.job_service.create_job`` could not be reused for stage 1: it takes
``page_ids`` and refuses an empty list, and a keyword search has no pages until
it has run. The INSERT in :func:`start_run` is deliberately the same shape as
that function's. Stage 2 DOES go through it.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import config as app_config
from . import db
from .meta_links import (
    is_numeric_page_id,
    meta_ads_library_url,
    meta_keyword_search_url,
    normalize_name,
)
from .time_utils import utc_now, utc_shift

log = logging.getLogger("adspy2.keyword")

# --- v1's form vocabulary, verbatim from tabs/keyword_research/ui.py --------
COUNTRIES = (("IN", "India"), ("US", "United States"), ("GB", "United Kingdom"), ("ALL", "All"))
AD_STATUSES = (("active", "Active"), ("all", "All"), ("inactive", "Inactive"))
PLATFORMS = (
    ("all", "All"),
    ("facebook", "Facebook"),
    ("instagram", "Instagram"),
    ("messenger", "Messenger"),
    ("audience_network", "Audience Network"),
)
MEDIA_TYPES = (("all", "All"), ("video", "Video"), ("image", "Image"), ("carousel", "Carousel"))
MONITOR_CHOICES = (("off", "Monitor off"), ("daily", "Daily"), ("weekly", "Weekly"))

DEPTH_DEFAULT, DEPTH_MIN, DEPTH_MAX = 500, 100, 5000
MAX_PAGES_DEFAULT, MAX_PAGES_MIN, MAX_PAGES_MAX = 100, 1, 1000
BULK_LIMIT = 100
PER_PAGE = 50

QUEUE_STATUSES = ("pending", "running")
OPEN_JOB_STATUSES = ("pending", "claimed", "running")
TERMINAL_JOB_STATUSES = ("completed", "failed", "cancelled")

# The review list's vocabulary (007's CHECK). Order = the tabs on the screen.
REVIEW_STATUSES = ("new", "accepted", "ignored", "queued", "scanned", "unscannable")
REVIEW_ACTIONS = ("accept", "ignore", "reset", "scan")

# Settings-table keys. Both are read with a default so an unset setting is
# never an error; the Keyword screen writes the first one through the
# automation form.
AUTO_SCAN_MIN_ADS_SETTING = "keyword.auto_scan.min_ads"
AUTO_SCAN_MIN_ADS_DEFAULT = 3
STAGE2_SKIP_VERIFIED_DAYS_SETTING = "keyword.stage2.skip_verified_days"
STAGE2_SKIP_VERIFIED_DAYS_DEFAULT = 3

# The caps ride in the search URL's FRAGMENT. The claim payload has no field
# for them (``_serialize_job_for_worker`` reads estimatedResults off ``pages``,
# which a keyword target does not have), and a fragment is the one part of a
# URL the browser never sends to Facebook. The extension reads them off
# ``target.pageUrl`` in the service worker (extension/bg/scrapeTarget.js,
# ``keywordCaps``) and stops the run with ``depth_reached`` / ``pages_reached``.
KEYWORD_CAP_ADS_PARAM = "adspy_max_ads"
KEYWORD_CAP_PAGES_PARAM = "adspy_max_pages"

# The Ad Library's own vocabulary for the two filters it accepts on a keyword
# search (v1 sent both: meta_main14/services/queue_service.py:863). Carousel is
# not an Ad Library media filter — it is detected per ad by the extractor — so
# a "carousel" search runs as "all" and the stored filter stays honest.
_FB_ACTIVE_STATUS = {"active": "active", "inactive": "inactive", "all": "all"}
_FB_MEDIA_TYPE = {"all": "all", "video": "video", "image": "image", "carousel": "all"}

# jobs.status -> keyword_runs.status. keyword_runs has its own CHECK and does
# not know the word "claimed".
JOB_STATUS_TO_RUN = {
    "pending": "pending",
    "claimed": "running",
    "running": "running",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}

# Recurring monitors have no column in 002 (see module docstring in
# app/routes/keyword.py and the build report). They live in `settings` under
# this prefix until a migration gives keyword_queries the two columns.
MONITOR_SETTING = "keyword.monitor.{query_id}"

_DOMAIN_RE = re.compile(r"^(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9\-]*\.[a-z]{2,}(?:\.[a-z]{2,})?)/?", re.I)
# ingest's warning line for ads it skipped because no page could be resolved.
_SKIPPED_NO_PAGE_RE = re.compile(r"^(\d+) ad\(s\) skipped: no resolvable page identity")


class KeywordError(Exception):
    """Anything the screen should show the owner as a flash message."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _rows(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(row) for row in db.fetch_all(sql, params)]


def _row(sql: str, params: Iterable[Any] = ()) -> dict | None:
    row = db.fetch_one(sql, params)
    return dict(row) if row is not None else None


def _placeholders(values: Sequence[Any]) -> str:
    return ",".join("?" for _ in values)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _clamp(value: Any, default: int, low: int, high: int) -> int:
    number = _int(value, default)
    return max(low, min(high, number))


def _choice(value: Any, options: Sequence[tuple[str, str]], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in {key for key, _ in options} else default


def _chunks(values: Sequence[Any], size: int = 400) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _setting_int(key: str, default: int) -> int:
    try:
        row = _row("SELECT value FROM settings WHERE key=?", (key,))
    except sqlite3.Error:  # no database yet (pure-function callers, scripts)
        return default
    return _int(row["value"], default) if row else default


def auto_scan_min_ads_default() -> int:
    """The owner's default threshold for bulk mode (setting, default 3)."""
    return max(1, _setting_int(AUTO_SCAN_MIN_ADS_SETTING, AUTO_SCAN_MIN_ADS_DEFAULT))


def set_auto_scan_min_ads_default(value: Any) -> int:
    number = _clamp(value, AUTO_SCAN_MIN_ADS_DEFAULT, 1, 1000)
    with db.transaction():
        db.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (AUTO_SCAN_MIN_ADS_SETTING, str(number), utc_now()),
        )
    return number


def looks_like_domain(term: Any) -> str:
    """The bare domain when `term` reads like a website, else ''.

    v1 switches the Research tab into "website research" mode on this test.
    """
    text = str(term or "").strip()
    if not text or " " in text:
        return ""
    match = _DOMAIN_RE.match(text)
    return match.group(1).lower() if match else ""


def parse_keyword(value: Any) -> str:
    """A keyword, or the ``q=`` out of a pasted Ad Library search URL.

    v1 accepts either in the same box ("Astrology app or Meta Ads Library
    search URL"), so this screen has to as well.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower().startswith(("http://", "https://")):
        from urllib.parse import parse_qs, unquote_plus, urlparse

        parsed = urlparse(text)
        query = parse_qs(parsed.query)
        for key in ("q", "search_terms", "query"):
            if query.get(key):
                return unquote_plus(str(query[key][0])).strip()[:200]
        return ""
    return text[:200]


def split_bulk(value: Any) -> list[str]:
    """One keyword per line, de-duplicated, capped at v1's 100."""
    out: list[str] = []
    seen: set[str] = set()
    for line in str(value or "").splitlines():
        keyword = parse_keyword(line)
        fold = keyword.casefold()
        if keyword and fold not in seen:
            seen.add(fold)
            out.append(keyword)
        if len(out) >= BULK_LIMIT:
            break
    return out


def filters_from_form(form: Any) -> dict[str, Any]:
    """The five filter values + the two depth numbers, sanitised (v1's set)."""
    return {
        "country": _choice(form.get("country"), COUNTRIES, "IN"),
        "ad_status": _choice(form.get("ad_status"), AD_STATUSES, "active"),
        "platform": _choice(form.get("platform"), PLATFORMS, "all"),
        "media_type": _choice(form.get("media_type"), MEDIA_TYPES, "all"),
        "default_depth": _clamp(form.get("depth"), DEPTH_DEFAULT, DEPTH_MIN, DEPTH_MAX),
        "max_pages": _clamp(form.get("max_pages"), MAX_PAGES_DEFAULT, MAX_PAGES_MIN, MAX_PAGES_MAX),
    }


def automation_from_form(form: Any) -> dict[str, Any]:
    """The bulk-mode pair from the add-search form: ``auto_scan`` (checkbox)
    and ``auto_scan_min_ads`` (threshold, defaulting to the owner's setting).
    Kept apart from :func:`filters_from_form` because these are not part of a
    saved search's identity."""
    return {
        "auto_scan": str(form.get("auto_scan") or "") in {"1", "on", "true"},
        "auto_scan_min_ads": _clamp(form.get("auto_scan_min_ads"), auto_scan_min_ads_default(), 1, 1000),
    }


# ---------------------------------------------------------------------------
# the search URL the extension opens
# ---------------------------------------------------------------------------
def keyword_search_url(
    keyword: Any,
    *,
    country: str = "IN",
    ad_status: str = "active",
    media_type: str = "all",
    platform: str = "all",
    depth: int = 0,
    max_pages: int = 0,
) -> str:
    """The Ad Library keyword-search URL with the saved search's filters
    applied and the run's caps in the fragment.

    Builds on :func:`app.meta_links.meta_keyword_search_url` (which knows the
    base shape) and adds what v1 sent — ``active_status`` and ``media_type`` —
    plus ``publisher_platforms[0]`` when a platform is chosen. The caps
    (``#adspy_max_ads=…&adspy_max_pages=…``) never reach Facebook: fragments
    are not sent on the wire. The extension reads them off ``target.pageUrl``.
    """
    base = meta_keyword_search_url(keyword, str(country or "IN"))
    if not base:
        return ""
    parts = urlsplit(base)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    params["active_status"] = _FB_ACTIVE_STATUS.get(str(ad_status or "").lower(), "active")
    params["media_type"] = _FB_MEDIA_TYPE.get(str(media_type or "").lower(), "all")
    platform_key = str(platform or "all").lower()
    if platform_key != "all" and platform_key in {key for key, _ in PLATFORMS}:
        params["publisher_platforms[0]"] = platform_key
    caps = {}
    if _int(depth) > 0:
        caps[KEYWORD_CAP_ADS_PARAM] = _int(depth)
    if _int(max_pages) > 0:
        caps[KEYWORD_CAP_PAGES_PARAM] = _int(max_pages)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), urlencode(caps)))


def search_url_caps(url: Any) -> dict[str, int]:
    """``{maxAds, maxPages}`` read back out of a keyword-search URL's fragment
    (0 = no cap). Mirrors ``keywordCaps`` in extension/bg/scrapeTarget.js."""
    try:
        fragment = urlsplit(str(url or "")).fragment
    except ValueError:
        fragment = ""
    values = dict(parse_qsl(fragment, keep_blank_values=True))
    return {
        "maxAds": max(0, _int(values.get(KEYWORD_CAP_ADS_PARAM), 0)),
        "maxPages": max(0, _int(values.get(KEYWORD_CAP_PAGES_PARAM), 0)),
    }


# ---------------------------------------------------------------------------
# queries (the saved searches)
# ---------------------------------------------------------------------------
def upsert_query(keyword: str, *, favorite: bool = False, **filters: Any) -> int:
    """Return the id of the saved search for this keyword+filter combination,
    creating it when it is new. The UNIQUE in 002 is the identity."""
    keyword = str(keyword or "").strip()[:200]
    if not keyword:
        raise KeywordError("A search needs a keyword.")

    values = {
        "country": "IN", "ad_status": "active", "platform": "all",
        "media_type": "all", "default_depth": DEPTH_DEFAULT,
        "max_pages": MAX_PAGES_DEFAULT,
    }
    values.update({k: v for k, v in filters.items() if k in values})
    now = utc_now()

    with db.transaction():
        db.execute(
            """
            INSERT INTO keyword_queries(keyword, country, ad_status, platform, media_type,
                                        default_depth, max_pages, sort_mode, is_saved,
                                        is_favorite, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,'relevance',?,?,?,?)
            ON CONFLICT(keyword, country, ad_status, platform, media_type) DO UPDATE SET
                default_depth = excluded.default_depth,
                max_pages     = excluded.max_pages,
                is_saved      = MAX(keyword_queries.is_saved, excluded.is_saved),
                is_favorite   = MAX(keyword_queries.is_favorite, excluded.is_favorite),
                updated_at    = excluded.updated_at
            """,
            (
                keyword, values["country"], values["ad_status"], values["platform"],
                values["media_type"], values["default_depth"], values["max_pages"],
                1 if favorite else 0, 1 if favorite else 0, now, now,
            ),
        )
        row = db.fetch_one(
            """
            SELECT id FROM keyword_queries
            WHERE keyword=? AND country=? AND ad_status=? AND platform=? AND media_type=?
            """,
            (keyword, values["country"], values["ad_status"], values["platform"], values["media_type"]),
        )
    return int(row["id"])


def set_favorite(query_id: int, on: bool) -> bool:
    now = utc_now()
    with db.transaction():
        db.execute(
            "UPDATE keyword_queries SET is_saved=?, is_favorite=?, updated_at=? WHERE id=?",
            (1 if on else 0, 1 if on else 0, now, int(query_id)),
        )
    return bool(on)


def get_monitor(query_id: int) -> str:
    row = _row("SELECT value FROM settings WHERE key=?", (MONITOR_SETTING.format(query_id=int(query_id)),))
    value = str(row["value"]) if row else "off"
    return value if value in {key for key, _ in MONITOR_CHOICES} else "off"


def set_monitor(query_id: int, frequency: str) -> str:
    frequency = _choice(frequency, MONITOR_CHOICES, "off")
    now = utc_now()
    with db.transaction():
        db.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (MONITOR_SETTING.format(query_id=int(query_id)), frequency, now),
        )
    return frequency


_LIBRARY_SQL = """
    SELECT q.id                              AS query_id,
           q.keyword, q.country, q.ad_status, q.platform, q.media_type,
           q.default_depth, q.max_pages, q.is_saved, q.is_favorite, q.created_at,
           COUNT(r.id)                       AS run_count,
           MAX(r.requested_at)               AS last_requested_at,
           COALESCE(SUM(r.ads_scanned), 0)   AS cumulative_ads_scanned,
           COALESCE(SUM(r.represented_ads), 0) AS cumulative_represented_ads,
           (SELECT COUNT(DISTINCT kr.page_id) FROM keyword_results kr
              JOIN keyword_runs r2 ON r2.id = kr.run_id
             WHERE r2.query_id = q.id)       AS cumulative_unique_pages,
           (SELECT COUNT(*) FROM keyword_discovered_pages d
             WHERE d.query_id = q.id)        AS discovered_pages,
           (SELECT COUNT(*) FROM keyword_discovered_pages d
             WHERE d.query_id = q.id AND d.review_status = 'new') AS discovered_new,
           (SELECT COUNT(*) FROM keyword_discovered_pages d
             WHERE d.query_id = q.id AND d.review_status = 'scanned') AS discovered_scanned,
           COALESCE((SELECT a.auto_scan FROM keyword_query_automation a
                      WHERE a.query_id = q.id), 0) AS auto_scan,
           (SELECT r3.status FROM keyword_runs r3
             WHERE r3.query_id = q.id ORDER BY r3.requested_at DESC, r3.id DESC LIMIT 1)
                                             AS latest_status,
           (SELECT r4.id FROM keyword_runs r4
             WHERE r4.query_id = q.id ORDER BY r4.requested_at DESC, r4.id DESC LIMIT 1)
                                             AS latest_run_id
    FROM keyword_queries q
    LEFT JOIN keyword_runs r ON r.query_id = q.id
    {where}
    GROUP BY q.id
    ORDER BY COALESCE(MAX(r.requested_at), q.created_at) DESC, q.id DESC
    LIMIT ? OFFSET ?
"""


def list_queries(*, saved_only: bool = False, page: int = 1, per_page: int = PER_PAGE) -> dict:
    """One page of the List / Favorites library table."""
    where = "WHERE q.is_saved = 1" if saved_only else ""
    total = _int(
        (_row(f"SELECT COUNT(*) AS n FROM keyword_queries q {where}") or {}).get("n"), 0
    )
    page = max(1, _int(page, 1))
    pages = max(1, -(-total // per_page)) if total else 1
    page = min(page, pages)
    items = _rows(_LIBRARY_SQL.format(where=where), (per_page, (page - 1) * per_page))
    for item in items:
        item["latest_status"] = item.get("latest_status") or "not_run"
        item["monitor_frequency"] = get_monitor(item["query_id"])
        item["auto_scan"] = bool(item.get("auto_scan"))
    return {
        "items": items, "total": total, "page": page,
        "pages": pages, "per_page": per_page,
    }


def get_query(query_id: int) -> dict | None:
    rows = _rows(_LIBRARY_SQL.format(where="WHERE q.id = ?"), (int(query_id), 1, 0))
    if not rows:
        return None
    item = rows[0]
    item["latest_status"] = item.get("latest_status") or "not_run"
    item["monitor_frequency"] = get_monitor(item["query_id"])
    item["auto_scan"] = bool(item.get("auto_scan"))
    return item


# ---------------------------------------------------------------------------
# the research queue = pending runs
# ---------------------------------------------------------------------------
def enqueue(keyword: str, *, favorite: bool = False, **filters: Any) -> dict:
    """Add one search to the research queue. Returns {query_id, run_id}.

    ``keyword_runs`` is UNIQUE(query_id, requested_at) and timestamps are
    second-precision, so queueing the same search twice inside one second (the
    bulk form does exactly that) would collide. Each retry moves the stamp one
    second forward, which keeps the column both unique and sortable.

    ``auto_scan`` / ``auto_scan_min_ads`` (from :func:`filters_from_form`) are
    not filters — they are the saved search's automation and land in
    ``keyword_query_automation`` through :func:`set_auto_scan`.
    """
    auto_scan = filters.pop("auto_scan", None)
    min_ads = filters.pop("auto_scan_min_ads", None)
    query_id = upsert_query(keyword, favorite=favorite, **filters)
    if auto_scan is not None:
        set_auto_scan(query_id, bool(auto_scan), min_ads)
    with db.transaction():
        for attempt in range(10):
            try:
                cursor = db.execute(
                    """
                    INSERT INTO keyword_runs(query_id, job_id, status, requested_at)
                    VALUES(?, NULL, 'pending', ?)
                    """,
                    (query_id, utc_shift(attempt)),
                )
                return {"query_id": query_id, "run_id": int(cursor.lastrowid)}
            except sqlite3.IntegrityError:
                continue
    raise KeywordError("Could not add that search to the queue — try again.")


def queue_items() -> list[dict]:
    """The Research-queue table: every run that has not finished."""
    return _rows(
        f"""
        SELECT r.id AS id, r.query_id, r.status, r.requested_at AS created_at,
               r.job_id, q.keyword, q.country, q.ad_status, q.platform, q.media_type,
               q.default_depth AS depth, q.max_pages,
               j.status AS job_status, j.label AS job_label,
               COALESCE(a.auto_scan, 0) AS auto_scan, COALESCE(a.min_ads, 0) AS min_ads,
               r.unique_pages, r.ads_scanned
        FROM keyword_runs r
        JOIN keyword_queries q ON q.id = r.query_id
        LEFT JOIN jobs j ON j.id = r.job_id
        LEFT JOIN keyword_query_automation a ON a.query_id = q.id
        WHERE r.status IN ({_placeholders(QUEUE_STATUSES)})
        ORDER BY r.id DESC
        """,
        QUEUE_STATUSES,
    )


def remove_queue_item(run_id: int) -> None:
    """Drop a queued search. A run that already has a job cancels the job too —
    leaving the job behind would scrape a search nobody is waiting for."""
    run = _row("SELECT id, job_id, status FROM keyword_runs WHERE id=?", (int(run_id),))
    if run is None:
        raise KeywordError(f"Queue item #{run_id} is gone already.")
    if run["job_id"]:
        try:
            from . import job_service

            job_service.cancel_job(int(run["job_id"]))
        except Exception as exc:  # pragma: no cover - job already finished
            log.info("cancel of job %s for run %s failed: %r", run["job_id"], run_id, exc)
    with db.transaction():
        db.execute("DELETE FROM keyword_runs WHERE id=?", (int(run_id),))


def start_run(run_id: int) -> int:
    """Dispatch a queued search: create the ``keyword`` job the extension claims.

    One job, one target, ``page_id`` NULL — see the module docstring for why
    that shape is the thing keeping ingest's two keyword rules in force.
    """
    run = _row(
        """
        SELECT r.id, r.job_id, r.status, q.keyword, q.country, q.ad_status, q.platform,
               q.media_type, q.default_depth, q.max_pages
        FROM keyword_runs r JOIN keyword_queries q ON q.id = r.query_id
        WHERE r.id = ?
        """,
        (int(run_id),),
    )
    if run is None:
        raise KeywordError(f"Queue item #{run_id} does not exist.")
    if run["job_id"]:
        raise KeywordError(f"{run['keyword']} is already running as job #{run['job_id']}.")
    # Same freeze as job_service.create_job: while OLD DATA is the live dataset
    # no scan job may be created, or it would sit pending forever behind a
    # claim that answers `dataset_frozen`. Surfaced as a KeywordError so every
    # keyword route flashes it instead of 500ing.
    from . import dataset as _dataset

    if not _dataset.scan_writes_allowed():
        raise KeywordError(_dataset.frozen_message())

    keyword = str(run["keyword"])
    now = utc_now()
    depth = _int(run["default_depth"], DEPTH_DEFAULT)
    search_url = keyword_search_url(
        keyword,
        country=str(run["country"] or "IN"),
        ad_status=str(run["ad_status"] or "active"),
        media_type=str(run["media_type"] or "all"),
        platform=str(run["platform"] or "all"),
        depth=depth,
        max_pages=_int(run["max_pages"], MAX_PAGES_DEFAULT),
    )

    with db.transaction():
        cursor = db.execute(
            """
            INSERT INTO jobs(job_type, status, idempotency_key, label, retryable,
                             retry_count, max_retries, targets_total, targets_done,
                             created_at, updated_at)
            VALUES('keyword', 'pending', ?, ?, 0, 0, ?, 1, 0, ?, ?)
            """,
            (uuid.uuid4().hex, f"Keyword: {keyword}", int(app_config.JOB_MAX_RETRIES), now, now),
        )
        job_id = int(cursor.lastrowid)
        db.execute(
            """
            INSERT INTO job_targets(job_id, position, page_id, platform_page_id,
                                    page_url, label, status, estimated_results)
            VALUES(?, 1, NULL, '', ?, ?, 'pending', ?)
            """,
            (job_id, search_url, keyword, depth),
        )
        db.execute(
            "UPDATE keyword_runs SET job_id=?, status='pending' WHERE id=?",
            (job_id, int(run_id)),
        )
        db.execute(
            "UPDATE keyword_queries SET last_run_at=?, updated_at=? "
            "WHERE id=(SELECT query_id FROM keyword_runs WHERE id=?)",
            (now, now, int(run_id)),
        )
    return job_id


def research_now(keyword: str, *, favorite: bool = False, **filters: Any) -> dict:
    """Queue + dispatch in one gesture (the detail screen's "Add to queue")."""
    created = enqueue(keyword, favorite=favorite, **filters)
    created["job_id"] = start_run(created["run_id"])
    return created


# ---------------------------------------------------------------------------
# keeping runs honest without touching ingest
# ---------------------------------------------------------------------------
def sync_runs_from_jobs() -> int:
    """Roll each dispatched run's job state and counters back onto the run,
    and materialise its pages.

    ``app/ingest.py`` and ``app/job_service.py`` know nothing about
    ``keyword_runs`` and must not: they are the shared write path. So the
    keyword screen reconciles on read instead — one UPDATE per open run,
    driven entirely by ``jobs``/``job_targets``/``job_batches`` — and
    :func:`rebuild_run_results` turns the run's accepted batches into
    ``keyword_results`` + the review list. A finished run is rebuilt exactly
    once more after it finishes (``results_built_at < finished_at``).
    """
    open_runs = _rows(
        """
        SELECT r.id, r.status, r.job_id, r.finished_at AS run_finished_at,
               j.status AS job_status, j.started_at, j.finished_at, j.error AS job_error,
               c.results_built_at
        FROM keyword_runs r
        JOIN jobs j ON j.id = r.job_id
        LEFT JOIN keyword_run_chain c ON c.run_id = r.id
        WHERE r.job_id IS NOT NULL
          AND (r.status IN ('pending','running') OR r.finished_at IS NULL
               OR c.results_built_at IS NULL OR c.results_built_at < r.finished_at)
        """
    )
    if not open_runs:
        return 0

    changed = 0
    for run in open_runs:
        with db.transaction():
            stats = _row(
                """
                SELECT COALESCE(SUM(t.unique_ads), 0)      AS ads_scanned,
                       COALESCE(SUM(t.scrolls), 0)         AS scroll_count,
                       COALESCE(SUM(t.represented_ads), 0) AS represented_ads,
                       MAX(t.outcome)                      AS outcome,
                       MAX(t.message)                      AS message
                FROM job_targets t WHERE t.job_id = ?
                """,
                (int(run["job_id"]),),
            ) or {}
            status = JOB_STATUS_TO_RUN.get(str(run["job_status"] or ""), "pending")
            stop_reason = None
            if status == "failed":
                stop_reason = run["job_error"]
            elif status in {"completed", "cancelled"}:
                # The extension's final /status carries the outcome and the stop
                # reason ("partial" + "depth_reached"); both belong in the history.
                stop_reason = " - ".join(
                    str(x) for x in (stats.get("outcome"), stats.get("message")) if x
                ) or status
            duration = _duration_seconds(run["started_at"], run["finished_at"])
            db.execute(
                """
                UPDATE keyword_runs
                   SET status = ?, ads_scanned = ?, unique_library_ids = ?,
                       represented_ads = ?, scroll_count = ?,
                       started_at = COALESCE(started_at, ?),
                       finished_at = ?, duration_seconds = ?,
                       stop_reason = COALESCE(stop_reason, ?)
                 WHERE id = ?
                """,
                (
                    status,
                    _int(stats.get("ads_scanned")), _int(stats.get("ads_scanned")),
                    _int(stats.get("represented_ads")), _int(stats.get("scroll_count")),
                    run["started_at"], run["finished_at"], duration, stop_reason,
                    int(run["id"]),
                ),
            )
            # unique_pages is set by the rebuild from what actually landed.
            rebuild_run_results(int(run["id"]))
        changed += 1
    return changed


def _duration_seconds(started_at: Any, finished_at: Any) -> float:
    try:
        if started_at and finished_at:
            start = datetime.fromisoformat(str(started_at))
            end = datetime.fromisoformat(str(finished_at))
            return max(0.0, (end - start).total_seconds())
    except ValueError:
        pass
    return 0.0


def _skipped_from_receipt(receipt_json: Any) -> int:
    try:
        receipt = json.loads(receipt_json or "{}")
    except (TypeError, ValueError):
        return 0
    total = 0
    for warning in receipt.get("warnings") or [] if isinstance(receipt, dict) else []:
        match = _SKIPPED_NO_PAGE_RE.match(str(warning))
        if match:
            total += _int(match.group(1))
    return total


def _identity_kind(platform_page_id: Any) -> str:
    return "numeric" if is_numeric_page_id(platform_page_id) else "name_hash"


def _upsert_discovered(
    query_id: int,
    run_id: int | None,
    page: dict[str, Any],
    ads_seen: int,
    now: str,
    *,
    initial_status: str | None = None,
) -> None:
    """One row per (saved search, page) in the review list.

    ``times_seen`` counts RUNS, so re-running the rebuild for the same open run
    (which happens after every batch) does not inflate it: the increment only
    fires when ``last_seen_run_id`` changes. ``ads_seen`` is the max over runs.
    A name-hash page is born ``unscannable`` (no Ad Library URL to open); a
    row the owner already acted on keeps its status.
    """
    kind = _identity_kind(page.get("platform_page_id"))
    status = initial_status or ("unscannable" if kind == "name_hash" else "new")
    name = str(page.get("alias") or page.get("name") or "")[:300]
    db.execute(
        """
        INSERT INTO keyword_discovered_pages(
            query_id, page_id, platform_page_id, page_name, identity_kind,
            first_seen_run_id, last_seen_run_id, times_seen, ads_seen, review_status,
            created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,1,?,?,?,?)
        ON CONFLICT(query_id, page_id) DO UPDATE SET
            platform_page_id = excluded.platform_page_id,
            page_name        = CASE WHEN excluded.page_name <> '' THEN excluded.page_name
                                    ELSE keyword_discovered_pages.page_name END,
            identity_kind    = excluded.identity_kind,
            last_seen_run_id = excluded.last_seen_run_id,
            times_seen       = keyword_discovered_pages.times_seen
                               + CASE WHEN keyword_discovered_pages.last_seen_run_id IS excluded.last_seen_run_id
                                      THEN 0 ELSE 1 END,
            ads_seen         = MAX(keyword_discovered_pages.ads_seen, excluded.ads_seen),
            review_status    = CASE
                WHEN excluded.identity_kind = 'name_hash'
                     AND keyword_discovered_pages.review_status = 'new' THEN 'unscannable'
                WHEN excluded.identity_kind = 'numeric'
                     AND keyword_discovered_pages.review_status = 'unscannable' THEN 'new'
                ELSE keyword_discovered_pages.review_status END,
            updated_at       = excluded.updated_at
        """,
        (
            int(query_id), int(page["page_id"]), str(page.get("platform_page_id") or ""),
            name, kind, run_id, run_id, int(ads_seen), status, now, now,
        ),
    )


def rebuild_run_results(run_id: int) -> dict[str, int]:
    """Materialise one run: ``job_batches`` -> ``keyword_results`` (per run,
    ranked v1's way) + ``keyword_discovered_pages`` (per saved search).

    Reads only what ingest already wrote — ``accepted_ad_ids_json`` in batch
    order gives the result positions, ``ads``/``pages`` give the grouping — so
    ingest stays ignorant of the keyword tables. Ranking is v1's
    ``refresh_keyword_results_conn``: matching ads desc, represented desc,
    first appearance asc.

    Two things happen here that belong to stage 1's contract:

      * pages this run CREATED are untracked (``is_tracked = 0``): a discovered
        page is a review-list entry, not a tracked competitor, until the owner
        accepts it. Pages that already existed keep their flag.
      * pages that have never had a page scan get honest ``total_ads`` /
        ``active_ads`` counters from the ads the sweep stored — a page scan
        owns those columns once it has run, so verified pages are untouched.
    """
    run = _row(
        """
        SELECT r.id, r.query_id, r.job_id, j.created_at AS job_created_at,
               COALESCE(j.started_at, j.created_at) AS job_started_at
        FROM keyword_runs r LEFT JOIN jobs j ON j.id = r.job_id
        WHERE r.id = ?
        """,
        (int(run_id),),
    )
    if run is None or not run["job_id"]:
        return {"pages": 0, "skipped": 0}
    job_id = int(run["job_id"])
    query_id = int(run["query_id"])
    now = utc_now()

    ordered = _rows(
        """
        SELECT je.value AS library_id
        FROM job_batches b, json_each(b.accepted_ad_ids_json) je
        WHERE b.job_id = ? AND b.status = 'accepted'
        ORDER BY b.batch_sequence, b.id, CAST(je.key AS INTEGER)
        """,
        (job_id,),
    )
    skipped = sum(
        _skipped_from_receipt(r["receipt_json"])
        for r in _rows(
            "SELECT receipt_json FROM job_batches WHERE job_id=? AND status='accepted'", (job_id,)
        )
    )

    first_position: dict[str, int] = {}
    for position, row in enumerate(ordered, start=1):
        first_position.setdefault(str(row["library_id"]), position)

    ads: list[dict] = []
    library_ids = list(first_position)
    for chunk in _chunks(library_ids):
        ads.extend(
            _rows(
                f"""
                SELECT a.library_id, a.page_id, a.status, a.start_date, a.represented_ad_count,
                       p.platform_page_id, p.name, p.alias
                FROM ads a JOIN pages p ON p.id = a.page_id
                WHERE a.library_id IN ({_placeholders(chunk)})
                """,
                chunk,
            )
        )

    groups: dict[int, dict[str, Any]] = {}
    for ad in ads:
        page_id = int(ad["page_id"])
        group = groups.setdefault(
            page_id,
            {
                "page_id": page_id,
                "platform_page_id": ad["platform_page_id"],
                "name": ad["name"], "alias": ad["alias"],
                "ads": 0, "represented": 0, "oldest": None, "first_position": None,
            },
        )
        group["ads"] += 1
        group["represented"] += max(1, _int(ad["represented_ad_count"], 1))
        start = ad["start_date"]
        if start and (group["oldest"] is None or str(start) < str(group["oldest"])):
            group["oldest"] = start
        position = first_position.get(str(ad["library_id"]))
        if position is not None and (group["first_position"] is None or position < group["first_position"]):
            group["first_position"] = position

    ranked = sorted(
        groups.values(),
        key=lambda g: (
            -g["ads"], -g["represented"],
            g["first_position"] is None, g["first_position"] or 0, g["page_id"],
        ),
    )

    with db.transaction():
        page_ids = [g["page_id"] for g in ranked]
        if page_ids:
            db.execute(
                f"DELETE FROM keyword_results WHERE run_id=? AND page_id NOT IN ({_placeholders(page_ids)})",
                (int(run_id), *page_ids),
            )
        else:
            db.execute("DELETE FROM keyword_results WHERE run_id=?", (int(run_id),))

        for rank, group in enumerate(ranked, start=1):
            active = _row(
                "SELECT COUNT(*) AS n FROM ads WHERE page_id=? AND status='active'", (group["page_id"],)
            ) or {}
            db.execute(
                """
                INSERT INTO keyword_results(
                    run_id, page_id, ads_matching, represented_matching, total_active_page_ads,
                    top_product, oldest_matching_ad_date, rank_position, first_result_position,
                    selected_for_analysis, analysis_status)
                VALUES(?,?,?,?,?,NULL,?,?,?,0,'not_selected')
                ON CONFLICT(run_id, page_id) DO UPDATE SET
                    ads_matching            = excluded.ads_matching,
                    represented_matching    = excluded.represented_matching,
                    total_active_page_ads   = excluded.total_active_page_ads,
                    oldest_matching_ad_date = excluded.oldest_matching_ad_date,
                    rank_position           = excluded.rank_position,
                    first_result_position   = excluded.first_result_position
                """,
                (
                    int(run_id), group["page_id"], group["ads"], group["represented"],
                    _int(active.get("n")), group["oldest"], rank,
                    group["first_position"] if group["first_position"] is not None else rank,
                ),
            )
            _upsert_discovered(query_id, int(run_id), group, group["ads"], now)

        if page_ids:
            # Stage 1 discovers, it does not track. Only pages this very run
            # brought into existence are untracked; anything that existed
            # before it (a tracked competitor that also matched the keyword, a
            # page added by hand) keeps its flag, and so does a page the owner
            # has since accepted. "Born by an ingest INSERT" is the signal:
            # ingest writes first_captured_at = created_at in one statement,
            # while a hand-added page has first_captured_at NULL until a batch
            # touches it (and then it is later than created_at). The job's
            # start time bounds it further - nothing this run stored can
            # predate its claim.
            db.execute(
                f"""
                UPDATE pages SET is_tracked = 0, updated_at = ?
                WHERE id IN ({_placeholders(page_ids)})
                  AND is_tracked = 1
                  AND first_captured_at IS NOT NULL
                  AND first_captured_at = created_at
                  AND created_at >= ?
                  AND last_verified_at IS NULL
                  AND NOT EXISTS (SELECT 1 FROM job_targets t WHERE t.page_id = pages.id)
                  AND NOT EXISTS (SELECT 1 FROM keyword_discovered_pages d
                                   WHERE d.page_id = pages.id
                                     AND d.review_status IN ('accepted','queued','scanned'))
                """,
                (now, *page_ids, str(run["job_started_at"] or run["job_created_at"] or now)),
            )
            # Honest counters for pages no page scan has ever published.
            db.execute(
                f"""
                UPDATE pages SET
                    total_ads  = (SELECT COUNT(*) FROM ads a WHERE a.page_id = pages.id),
                    active_ads = (SELECT COUNT(*) FROM ads a
                                   WHERE a.page_id = pages.id AND a.status = 'active'),
                    updated_at = ?
                WHERE id IN ({_placeholders(page_ids)}) AND last_verified_at IS NULL
                """,
                (now, *page_ids),
            )

        db.execute(
            """
            INSERT INTO keyword_run_chain(run_id, results_built_at, ads_skipped_no_page, updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(run_id) DO UPDATE SET
                results_built_at    = excluded.results_built_at,
                ads_skipped_no_page = excluded.ads_skipped_no_page,
                updated_at          = excluded.updated_at
            """,
            (int(run_id), now, int(skipped), now),
        )
        db.execute(
            "UPDATE keyword_runs SET unique_pages=? WHERE id=?", (len(ranked), int(run_id))
        )
    return {"pages": len(ranked), "skipped": int(skipped)}


def _materialise_legacy_runs() -> int:
    """v1-backfilled runs (``job_id IS NULL``) have ``keyword_results`` but no
    review-list rows. Fold them in once, oldest first, so the OLD dataset's
    saved searches show their pages under Discovered pages too. A page that
    already went through a page scan is filed as ``scanned``."""
    runs = _rows(
        """
        SELECT r.id, r.query_id FROM keyword_runs r
        LEFT JOIN keyword_run_chain c ON c.run_id = r.id
        WHERE r.job_id IS NULL AND c.run_id IS NULL
        ORDER BY r.requested_at, r.id
        """
    )
    if not runs:
        return 0
    now = utc_now()
    with db.transaction():
        for run in runs:
            results = _rows(
                """
                SELECT kr.page_id, kr.ads_matching, p.platform_page_id, p.name, p.alias,
                       p.last_verified_at
                FROM keyword_results kr JOIN pages p ON p.id = kr.page_id
                WHERE kr.run_id = ?
                """,
                (int(run["id"]),),
            )
            for result in results:
                initial = "scanned" if result.get("last_verified_at") else None
                _upsert_discovered(
                    int(run["query_id"]), int(run["id"]), result,
                    _int(result["ads_matching"]), now, initial_status=initial,
                )
            db.execute(
                """
                INSERT INTO keyword_run_chain(run_id, results_built_at, stage2_fired_at,
                                              stage2_note, updated_at)
                VALUES(?,?,?, 'imported from v1 - no stage 2', ?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (int(run["id"]), now, now, now),
            )
    return len(runs)


# ---------------------------------------------------------------------------
# stage 2 — scan the pages the owner accepted
# ---------------------------------------------------------------------------
def _blocked_page_ids(page_ids: Sequence[int]) -> set[int]:
    if not page_ids:
        return set()
    return {
        int(r["id"])
        for r in _rows(
            f"""
            SELECT p.id FROM pages p JOIN blocked_pages b ON b.platform_page_id = p.platform_page_id
            WHERE p.id IN ({_placeholders(page_ids)})
            """,
            page_ids,
        )
    }


def stage2_scan(
    query_id: int,
    page_ids: Iterable[Any],
    *,
    run_id: int | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Accept -> Track -> Scan for these discovered pages.

    Marks the pages tracked, then creates page_scan jobs for them through
    ``app.routes.queue.create_scan_job`` (which delegates to
    ``job_service.create_job``), so the duplicate guard (PRD P0.2) and the
    "no numeric Meta page id, no scan" filter both apply unchanged. One job
    never covers more than the service's SCAN_JOB_MAX_PAGES pages. Blocked
    pages are dropped first. The review rows are then marked ``queued`` with
    their job id (a page already queued by another live job is marked queued
    against THAT job), and rows create_scan_job could not open become
    ``unscannable``.

    Returns create_scan_job's dict plus ``blocked`` — ``job_id`` is None when
    nothing was left to queue and ``reason`` says why.
    """
    from .routes.queue import create_scan_job  # a route module, but the one owner of job creation for the UI

    query_id = int(query_id)
    wanted: list[int] = []
    for value in page_ids:
        try:
            page_id = int(value)
        except (TypeError, ValueError):
            continue
        if page_id not in wanted:
            wanted.append(page_id)

    blocked = _blocked_page_ids(wanted)
    ids = [p for p in wanted if p not in blocked]
    empty = {"job_id": None, "queued": [], "skipped": [], "unscannable": [],
             "blocked": sorted(blocked), "reason": "no_pages"}
    if not ids:
        return {**empty, "reason": "all_blocked" if blocked else "no_pages"}

    now = utc_now()
    with db.transaction():
        db.execute(
            f"UPDATE pages SET is_tracked = 1, updated_at = ? WHERE id IN ({_placeholders(ids)}) AND is_tracked = 0",
            (now, *ids),
        )

    keyword = (_row("SELECT keyword FROM keyword_queries WHERE id=?", (query_id,)) or {}).get("keyword", "")
    result = create_scan_job(ids, label=(label or "").strip() or f"Keyword scan: {keyword}")
    result["blocked"] = sorted(blocked)

    queued_ids = [int(p["id"]) for p in result.get("queued") or []]
    unscannable_ids = [int(p["id"]) for p in result.get("unscannable") or []]
    skipped_ids = [int(p["id"]) for p in result.get("skipped") or [] if int(p["id"]) not in unscannable_ids]

    with db.transaction():
        if queued_ids and result.get("job_id"):
            page_job_ids = result.get("page_job_ids") or {}
            by_job: dict[int, list[int]] = {}
            for page_id in queued_ids:
                jid = int(page_job_ids.get(page_id) or result["job_id"])
                by_job.setdefault(jid, []).append(page_id)
            for jid, pids in by_job.items():
                db.execute(
                    f"""
                    UPDATE keyword_discovered_pages
                       SET review_status = 'queued', scan_job_id = ?, reviewed_at = ?, updated_at = ?
                     WHERE query_id = ? AND page_id IN ({_placeholders(pids)})
                    """,
                    (jid, now, now, query_id, *pids),
                )
        if unscannable_ids:
            db.execute(
                f"""
                UPDATE keyword_discovered_pages
                   SET review_status = 'unscannable', updated_at = ?
                 WHERE query_id = ? AND page_id IN ({_placeholders(unscannable_ids)})
                   AND review_status <> 'scanned'
                """,
                (now, query_id, *unscannable_ids),
            )
        if skipped_ids:
            # Already queued by some other live job: point the row at that job
            # so the roll-up below still lands it on "scanned".
            for row in _rows(
                f"""
                SELECT t.page_id, MAX(t.job_id) AS job_id
                FROM job_targets t JOIN jobs j ON j.id = t.job_id
                WHERE t.page_id IN ({_placeholders(skipped_ids)})
                  AND j.status IN ({_placeholders(OPEN_JOB_STATUSES)})
                  AND t.status IN ('pending','running')
                GROUP BY t.page_id
                """,
                (*skipped_ids, *OPEN_JOB_STATUSES),
            ):
                db.execute(
                    """
                    UPDATE keyword_discovered_pages
                       SET review_status = 'queued', scan_job_id = ?, reviewed_at = ?, updated_at = ?
                     WHERE query_id = ? AND page_id = ?
                    """,
                    (int(row["job_id"]), now, now, query_id, int(row["page_id"])),
                )
    if run_id is not None:
        with db.transaction():
            db.execute(
                """
                INSERT INTO keyword_run_chain(run_id, stage2_job_id, stage2_fired_at, updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                    stage2_job_id   = COALESCE(excluded.stage2_job_id, keyword_run_chain.stage2_job_id),
                    stage2_fired_at = excluded.stage2_fired_at,
                    updated_at      = excluded.updated_at
                """,
                (int(run_id), result.get("job_id"), now, now),
            )
    return result


def scan_all(query_id: int, *, statuses: Sequence[str] = ("new", "accepted")) -> dict[str, Any]:
    """The bulk button: stage 2 for every numeric-id discovered page in these
    review states. Blocked and already-queued pages fall out inside
    :func:`stage2_scan`."""
    statuses = tuple(s for s in statuses if s in REVIEW_STATUSES) or ("new", "accepted")
    rows = _rows(
        f"""
        SELECT page_id FROM keyword_discovered_pages
        WHERE query_id = ? AND identity_kind = 'numeric'
          AND review_status IN ({_placeholders(statuses)})
        ORDER BY ads_seen DESC, page_id
        """,
        (int(query_id), *statuses),
    )
    if not rows:
        return {"job_id": None, "queued": [], "skipped": [], "unscannable": [],
                "blocked": [], "reason": "no_pages"}
    return stage2_scan(query_id, [r["page_id"] for r in rows])


def review_page(query_id: int, page_id: int, action: str) -> dict[str, Any]:
    """One row's review action: accept (-> tracked), ignore, reset, scan."""
    action = str(action or "").strip().lower()
    if action not in REVIEW_ACTIONS:
        raise KeywordError(f"Unknown review action {action!r}.")
    row = _row(
        "SELECT * FROM keyword_discovered_pages WHERE query_id=? AND page_id=?",
        (int(query_id), int(page_id)),
    )
    if row is None:
        raise KeywordError("That page is not in this search's discovered list.")

    now = utc_now()
    if action == "scan":
        result = stage2_scan(query_id, [int(page_id)])
        return {"action": action, **result}

    if action == "accept":
        status = "accepted"
    elif action == "ignore":
        status = "ignored"
    else:
        status = "unscannable" if row["identity_kind"] == "name_hash" else "new"
    with db.transaction():
        db.execute(
            """
            UPDATE keyword_discovered_pages
               SET review_status = ?, reviewed_at = ?, updated_at = ?
             WHERE query_id = ? AND page_id = ?
            """,
            (status, now, now, int(query_id), int(page_id)),
        )
        if action == "accept":
            db.execute(
                "UPDATE pages SET is_tracked = 1, updated_at = ? WHERE id = ? AND is_tracked = 0",
                (now, int(page_id)),
            )
    return {"action": action, "review_status": status, "page_id": int(page_id)}


def get_automation(query_id: int) -> dict[str, Any]:
    row = _row("SELECT auto_scan, min_ads FROM keyword_query_automation WHERE query_id=?", (int(query_id),))
    if row is None:
        return {"auto_scan": False, "min_ads": auto_scan_min_ads_default(), "configured": False}
    return {"auto_scan": bool(row["auto_scan"]), "min_ads": max(1, _int(row["min_ads"], 1)), "configured": True}


def set_auto_scan(query_id: int, on: bool, min_ads: Any = None) -> dict[str, Any]:
    """Turn bulk mode on/off for a saved search.

    Turning it ON applies to runs that finish FROM NOW: runs that already
    completed are stamped as fired with a note, so a toggle never fans out a
    stage-2 job per historical run. The backlog is one click away ("Scan all
    new + accepted") and the owner sees exactly what it will queue.
    """
    current = get_automation(query_id)
    threshold = _clamp(min_ads, current["min_ads"], 1, 1000) if min_ads is not None else current["min_ads"]
    now = utc_now()
    with db.transaction():
        db.execute(
            """
            INSERT INTO keyword_query_automation(query_id, auto_scan, min_ads, updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(query_id) DO UPDATE SET
                auto_scan = excluded.auto_scan, min_ads = excluded.min_ads,
                updated_at = excluded.updated_at
            """,
            (int(query_id), 1 if on else 0, threshold, now),
        )
        if on:
            for run in _rows(
                """
                SELECT r.id FROM keyword_runs r
                LEFT JOIN keyword_run_chain c ON c.run_id = r.id
                WHERE r.query_id = ? AND r.status IN ('completed','failed','cancelled')
                  AND (c.run_id IS NULL OR c.stage2_fired_at IS NULL)
                """,
                (int(query_id),),
            ):
                db.execute(
                    """
                    INSERT INTO keyword_run_chain(run_id, stage2_fired_at, stage2_note, updated_at)
                    VALUES(?,?, 'auto-scan was turned on after this run finished', ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        stage2_fired_at = excluded.stage2_fired_at,
                        stage2_note     = excluded.stage2_note,
                        updated_at      = excluded.updated_at
                    """,
                    (int(run["id"]), now, now),
                )
    return {"auto_scan": bool(on), "min_ads": threshold}


def _roll_review_statuses() -> int:
    """queued -> scanned once the page's target is done; back to accepted when
    the job ended without scanning it (failed / skipped / cancelled), so the
    next Scan all picks it up again."""
    rows = _rows(
        """
        SELECT d.query_id, d.page_id, d.scan_job_id, j.status AS job_status, t.status AS target_status
        FROM keyword_discovered_pages d
        LEFT JOIN jobs j ON j.id = d.scan_job_id
        LEFT JOIN job_targets t ON t.job_id = d.scan_job_id AND t.page_id = d.page_id
        WHERE d.review_status = 'queued'
        """
    )
    if not rows:
        return 0
    now = utc_now()
    changed = 0
    with db.transaction():
        for row in rows:
            target_status = str(row.get("target_status") or "")
            job_status = str(row.get("job_status") or "")
            if target_status == "done":
                new_status = "scanned"
            elif row.get("scan_job_id") is None or job_status in TERMINAL_JOB_STATUSES:
                new_status = "accepted"
            else:
                continue
            db.execute(
                """
                UPDATE keyword_discovered_pages SET review_status = ?, updated_at = ?
                WHERE query_id = ? AND page_id = ? AND review_status = 'queued'
                """,
                (new_status, now, int(row["query_id"]), int(row["page_id"])),
            )
            changed += 1
    return changed


def stage2_candidates(query_id: int, min_ads: int, *, skip_verified_days: int | None = None) -> list[int]:
    """Pages bulk mode would queue right now: numeric id, not blocked, at
    least ``min_ads`` matching ads, in a state that wants a scan, and not
    verified by a page scan inside the skip window."""
    days = STAGE2_SKIP_VERIFIED_DAYS_DEFAULT if skip_verified_days is None else skip_verified_days
    cutoff = (datetime.now(UTC) - timedelta(days=max(0, int(days)))).replace(microsecond=0).isoformat()
    rows = _rows(
        """
        SELECT d.page_id FROM keyword_discovered_pages d
        JOIN pages p ON p.id = d.page_id
        LEFT JOIN blocked_pages b ON b.platform_page_id = p.platform_page_id
        WHERE d.query_id = ? AND d.identity_kind = 'numeric' AND b.id IS NULL
          AND d.review_status IN ('new','accepted','scanned')
          AND d.ads_seen >= ?
          AND (p.last_verified_at IS NULL OR p.last_verified_at < ?)
        ORDER BY d.ads_seen DESC, d.page_id
        """,
        (int(query_id), int(min_ads), cutoff),
    )
    return [int(r["page_id"]) for r in rows]


def _fire_auto_scans() -> int:
    """Stage 2 for every completed stage-1 run whose saved search has
    auto-scan on and which has not fired yet. Idempotent: the chain row is
    stamped even when zero pages qualified, so a run fires at most once."""
    due = _rows(
        """
        SELECT r.id AS run_id, r.query_id, q.keyword, a.min_ads
        FROM keyword_runs r
        JOIN keyword_queries q ON q.id = r.query_id
        JOIN keyword_query_automation a ON a.query_id = r.query_id AND a.auto_scan = 1
        LEFT JOIN keyword_run_chain c ON c.run_id = r.id
        WHERE r.job_id IS NOT NULL AND r.status = 'completed'
          AND (c.run_id IS NULL OR c.stage2_fired_at IS NULL)
        ORDER BY r.id
        """
    )
    fired = 0
    skip_days = _setting_int(STAGE2_SKIP_VERIFIED_DAYS_SETTING, STAGE2_SKIP_VERIFIED_DAYS_DEFAULT)
    for run in due:
        run_id = int(run["run_id"])
        min_ads = max(1, _int(run["min_ads"], AUTO_SCAN_MIN_ADS_DEFAULT))
        try:
            candidates = stage2_candidates(int(run["query_id"]), min_ads, skip_verified_days=skip_days)
            if candidates:
                result = stage2_scan(
                    int(run["query_id"]), candidates, run_id=run_id,
                    label=f"Keyword scan: {run['keyword']}",
                )
                note = (
                    f"auto-scan queued {len(result.get('queued') or [])} page(s)"
                    if result.get("job_id")
                    else f"auto-scan: nothing to queue ({result.get('reason')})"
                )
                if result.get("job_id"):
                    fired += 1
            else:
                note = f"auto-scan: no page had {min_ads}+ matching ads (or all were scanned recently)"
            now = utc_now()
            with db.transaction():
                db.execute(
                    """
                    INSERT INTO keyword_run_chain(run_id, stage2_fired_at, stage2_note, updated_at)
                    VALUES(?,?,?,?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        stage2_fired_at = COALESCE(keyword_run_chain.stage2_fired_at, excluded.stage2_fired_at),
                        stage2_note     = excluded.stage2_note,
                        updated_at      = excluded.updated_at
                    """,
                    (run_id, now, note[:400], now),
                )
        except Exception as exc:  # pragma: no cover - one bad run must not block the rest
            log.warning("auto-scan for run %s failed: %r", run_id, exc)
    return fired


def advance_chains() -> dict[str, int]:
    """The one call that keeps the two-stage flow moving. Cheap and idempotent;
    ``app/routes/keyword.py`` runs it before every ``/api/worker/claim``, after
    every ``/api/jobs/<id>/done`` and on every screen read.

      1. roll job state onto runs + rebuild their results (:func:`sync_runs_from_jobs`)
      2. fold v1-imported runs into the review list, once
      3. queued -> scanned / accepted from the stage-2 job's targets
      4. fire stage 2 for completed runs whose search has auto-scan on
    """
    stats = {"synced": 0, "legacy_runs": 0, "rolled": 0, "stage2_jobs": 0}
    stats["synced"] = sync_runs_from_jobs()
    stats["legacy_runs"] = _materialise_legacy_runs()
    stats["rolled"] = _roll_review_statuses()
    stats["stage2_jobs"] = _fire_auto_scans()
    return stats


# ---------------------------------------------------------------------------
# the review list + run history (what the Details screen paints)
# ---------------------------------------------------------------------------
def discovered_pages(query_id: int, status: str | None = None) -> list[dict]:
    where = "WHERE d.query_id = ?"
    params: list[Any] = [int(query_id)]
    if status in REVIEW_STATUSES:
        where += " AND d.review_status = ?"
        params.append(status)
    rows = _rows(
        f"""
        SELECT d.query_id, d.page_id, d.platform_page_id, d.page_name, d.identity_kind,
               d.first_seen_run_id, d.last_seen_run_id, d.times_seen, d.ads_seen,
               d.review_status, d.scan_job_id, d.reviewed_at, d.created_at, d.updated_at,
               p.name AS page_name_now, p.alias, p.url AS page_url, p.is_tracked,
               p.last_verified_at, p.current_scan_status,
               (SELECT COUNT(*) FROM ads a WHERE a.page_id = d.page_id) AS ads_known,
               (SELECT COUNT(*) FROM ads a WHERE a.page_id = d.page_id AND a.status = 'active') AS active_known,
               (SELECT MIN(a.start_date) FROM ads a
                 WHERE a.page_id = d.page_id AND a.start_date IS NOT NULL) AS oldest_ad,
               (SELECT GROUP_CONCAT(q2.keyword, ' | ')
                  FROM keyword_discovered_pages d2
                  JOIN keyword_queries q2 ON q2.id = d2.query_id
                 WHERE d2.page_id = d.page_id AND d2.query_id <> d.query_id) AS other_keywords,
               fr.requested_at AS first_seen_at,
               lr.requested_at AS last_seen_at,
               b.id AS block_id, b.reason AS block_reason,
               j.status AS scan_job_status
        FROM keyword_discovered_pages d
        JOIN pages p ON p.id = d.page_id
        LEFT JOIN keyword_runs fr ON fr.id = d.first_seen_run_id
        LEFT JOIN keyword_runs lr ON lr.id = d.last_seen_run_id
        LEFT JOIN blocked_pages b ON b.platform_page_id = p.platform_page_id
        LEFT JOIN jobs j ON j.id = d.scan_job_id
        {where}
        ORDER BY d.ads_seen DESC, d.times_seen DESC, d.page_id
        """,
        params,
    )
    for row in rows:
        row["display_name"] = str(
            row.get("alias") or row.get("page_name_now") or row.get("page_name") or row.get("platform_page_id") or "Unknown page"
        )
        row["is_blocked"] = row.get("block_id") is not None
        row["meta_page_id"] = str(row["platform_page_id"]) if row.get("identity_kind") == "numeric" else ""
        row["library_url"] = meta_ads_library_url(row.get("platform_page_id"), row.get("page_url"))
        row["is_tracked"] = bool(row.get("is_tracked"))
        row["other_keywords"] = [k for k in str(row.get("other_keywords") or "").split(" | ") if k]
        row["can_scan"] = bool(
            row["meta_page_id"] and not row["is_blocked"] and row["review_status"] != "queued"
        )
    return rows


def discovered_counts(query_id: int) -> dict[str, int]:
    counts = {status: 0 for status in REVIEW_STATUSES}
    for row in _rows(
        "SELECT review_status, COUNT(*) AS n FROM keyword_discovered_pages WHERE query_id=? GROUP BY review_status",
        (int(query_id),),
    ):
        counts[str(row["review_status"])] = _int(row["n"])
    counts["all"] = sum(counts[s] for s in REVIEW_STATUSES)
    counts["scannable"] = _int(
        (_row(
            """
            SELECT COUNT(*) AS n FROM keyword_discovered_pages d
            JOIN pages p ON p.id = d.page_id
            LEFT JOIN blocked_pages b ON b.platform_page_id = p.platform_page_id
            WHERE d.query_id = ? AND d.identity_kind = 'numeric' AND b.id IS NULL
              AND d.review_status IN ('new','accepted')
            """,
            (int(query_id),),
        ) or {}).get("n")
    )
    return counts


def run_history(query_id: int) -> list[dict]:
    """Every run of a saved search with its stage-1 job, its stage-2 job and
    the materialisation stamp — the Run history panel."""
    rows = _rows(
        """
        SELECT r.id, r.status, r.ads_scanned, r.represented_ads, r.unique_pages, r.scroll_count,
               r.duration_seconds, r.stop_reason, r.requested_at, r.started_at, r.finished_at,
               r.job_id, j.status AS job_status, j.outcome AS job_outcome,
               c.results_built_at, c.ads_skipped_no_page, c.stage2_job_id, c.stage2_fired_at,
               c.stage2_note,
               s.status AS stage2_status, s.targets_done AS stage2_done, s.targets_total AS stage2_total
        FROM keyword_runs r
        LEFT JOIN jobs j ON j.id = r.job_id
        LEFT JOIN keyword_run_chain c ON c.run_id = r.id
        LEFT JOIN jobs s ON s.id = c.stage2_job_id
        WHERE r.query_id = ?
        ORDER BY r.requested_at DESC, r.id DESC
        """,
        (int(query_id),),
    )
    for row in rows:
        row["stage"] = (
            "scan" if row.get("stage2_job_id") else
            "discover" if row.get("job_id") else "imported"
        )
    return rows


# ---------------------------------------------------------------------------
# results (v1's ranked table, per run / cumulative)
# ---------------------------------------------------------------------------
_RESULT_COLUMNS = """
    kr.page_id, kr.ads_matching, kr.represented_matching, kr.total_active_page_ads,
    kr.top_product, kr.oldest_matching_ad_date, kr.rank_position,
    kr.first_result_position, kr.selected_for_analysis, kr.analysis_status,
    p.name AS page_name, p.alias AS page_alias, p.platform_page_id, p.url AS page_url,
    p.active_ads AS page_active_ads,
    (b.id IS NOT NULL) AS is_blocked, b.id AS block_id, b.reason AS block_reason
"""


def run_results(run_id: int) -> list[dict]:
    return _decorate_results(
        _rows(
            f"""
            SELECT {_RESULT_COLUMNS}
            FROM keyword_results kr
            JOIN pages p ON p.id = kr.page_id
            LEFT JOIN blocked_pages b ON b.platform_page_id = p.platform_page_id
            WHERE kr.run_id = ?
            ORDER BY COALESCE(kr.rank_position, 9999), kr.ads_matching DESC
            """,
            (int(run_id),),
        )
    )


def cumulative_results(query_id: int) -> list[dict]:
    """Every page this saved search has ever surfaced, each carrying the
    numbers from the most recent run that saw it.

    v1's rule: "New runs add pages without removing earlier discoveries."
    SQLite's bare-column-with-MAX() picks the row of the newest run.
    """
    return _decorate_results(
        _rows(
            f"""
            SELECT {_RESULT_COLUMNS}, MAX(r.requested_at) AS last_run_at, r.id AS run_id
            FROM keyword_results kr
            JOIN keyword_runs r ON r.id = kr.run_id
            JOIN pages p ON p.id = kr.page_id
            LEFT JOIN blocked_pages b ON b.platform_page_id = p.platform_page_id
            WHERE r.query_id = ?
            GROUP BY kr.page_id
            ORDER BY COALESCE(kr.rank_position, 9999), kr.ads_matching DESC
            """,
            (int(query_id),),
        )
    )


def _decorate_results(rows: list[dict]) -> list[dict]:
    """Fill in v1's second line (the destination domain) and Top product.

    v2 has no ``pages.website_domain``; the honest equivalent is the domain of
    the product this page's ads most often point at.
    """
    for row in rows:
        row["display_name"] = str(row.get("page_alias") or row.get("page_name") or row.get("platform_page_id") or "Unknown page")
        row["is_blocked"] = bool(row.get("is_blocked"))
    page_ids = [int(r["page_id"]) for r in rows if r.get("page_id")]
    if not page_ids:
        return rows

    top: dict[int, dict] = {}
    for row in _rows(
        f"""
        SELECT a.page_id AS page_id, pr.domain AS domain,
               COALESCE(NULLIF(pr.display_name, ''), pr.normalized_name) AS product_name,
               COUNT(*) AS n
        FROM ad_products ap
        JOIN ads a      ON a.id = ap.ad_id
        JOIN products pr ON pr.id = ap.product_id
        WHERE a.page_id IN ({_placeholders(page_ids)})
        GROUP BY a.page_id, ap.product_id
        ORDER BY a.page_id, n DESC
        """,
        page_ids,
    ):
        top.setdefault(int(row["page_id"]), row)

    for row in rows:
        best = top.get(int(row["page_id"] or 0)) or {}
        row["website_domain"] = best.get("domain") or "No website stored"
        row["top_product"] = row.get("top_product") or best.get("product_name") or "—"
    return rows


def blocked_results(query_id: int | None = None, run_id: int | None = None) -> list[dict]:
    """v1's "N pages blocked-hidden from this search" strip."""
    if run_id:
        where, params = "kr.run_id = ?", (int(run_id),)
    elif query_id:
        where, params = "r.query_id = ?", (int(query_id),)
    else:
        return []
    return _rows(
        f"""
        SELECT b.id AS block_id, b.reason AS block_reason, b.platform_page_id,
               COALESCE(p.alias, p.name, b.page_name_snapshot) AS display_name,
               MAX(kr.ads_matching) AS ads_matching
        FROM keyword_results kr
        JOIN keyword_runs r ON r.id = kr.run_id
        JOIN pages p ON p.id = kr.page_id
        JOIN blocked_pages b ON b.platform_page_id = p.platform_page_id
        WHERE {where}
        GROUP BY b.id
        ORDER BY ads_matching DESC
        """,
        params,
    )


def block_page(page_id: int, reason: str = "") -> None:
    page = _row("SELECT id, platform_page_id, name, alias FROM pages WHERE id=?", (int(page_id),))
    if page is None:
        raise KeywordError(f"Page #{page_id} does not exist.")
    with db.transaction():
        db.execute(
            """
            INSERT INTO blocked_pages(platform_page_id, page_id, page_name_snapshot, reason, created_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(platform_page_id) DO UPDATE SET reason=excluded.reason
            """,
            (
                str(page["platform_page_id"] or ""), int(page["id"]),
                str(page["alias"] or page["name"] or ""),
                str(reason or "Blocked from Keyword Research")[:400], utc_now(),
            ),
        )


def unblock_page(block_id: int) -> None:
    with db.transaction():
        db.execute("DELETE FROM blocked_pages WHERE id=?", (int(block_id),))


def query_detail(
    query_id: int,
    *,
    run_id: int | None = None,
    view: str = "cumulative",
    review_status: str | None = None,
) -> dict | None:
    """Everything the Search Details tab paints."""
    query = get_query(query_id)
    if query is None:
        return None

    runs = _rows(
        """
        SELECT id, status, ads_scanned, represented_ads, unique_pages, scroll_count,
               duration_seconds, stop_reason, requested_at, started_at, finished_at, job_id
        FROM keyword_runs WHERE query_id=? ORDER BY requested_at DESC, id DESC
        """,
        (int(query_id),),
    )
    selected_run_id = int(run_id) if run_id else (_int(runs[0]["id"]) if runs else 0)
    latest = next((r for r in runs if _int(r["id"]) == selected_run_id), runs[0] if runs else {})
    cumulative = str(view or "cumulative").lower() != "run"

    results = cumulative_results(query_id) if cumulative else run_results(selected_run_id)
    visible = [r for r in results if not r["is_blocked"]]

    summary = {
        "unique_pages": len({int(r["page_id"]) for r in results}),
        "ads_scanned": sum(_int(r["ads_scanned"]) for r in runs),
        "represented_ads": sum(_int(r["represented_ads"]) for r in runs),
    }
    status_filter = review_status if review_status in REVIEW_STATUSES else None
    counts = discovered_counts(query_id)
    automation = get_automation(query_id)
    return {
        "query": query,
        "runs": runs,
        "latest_run": latest or {},
        "selected_run_id": selected_run_id,
        "view_mode": "cumulative" if cumulative else "run",
        "cumulative_summary": summary,
        "page_groups": visible,
        "blocked_page_groups": blocked_results(
            query_id=query_id if cumulative else None,
            run_id=None if cumulative else selected_run_id,
        ),
        "shared_domain_hint": _shared_domain_hint(visible),
        # the two-stage flow
        "discovered": discovered_pages(query_id, status_filter),
        "discovered_counts": counts,
        "review_status": status_filter or "all",
        "automation": automation,
        "stage2_candidates": len(stage2_candidates(query_id, automation["min_ads"])),
        "history": run_history(query_id),
        "open_runs": [r for r in runs if str(r.get("status")) in QUEUE_STATUSES],
    }


def _shared_domain_hint(rows: list[dict]) -> dict:
    """v1's "N pages share <domain>" chip — the Brand Groups nudge."""
    counts: dict[str, int] = {}
    for row in rows:
        domain = str(row.get("website_domain") or "")
        if domain and domain != "No website stored":
            counts[domain] = counts.get(domain, 0) + 1
    if not counts:
        return {}
    domain, pages = max(counts.items(), key=lambda item: item[1])
    return {"domain": domain, "pages": pages} if pages >= 2 else {}


def export_run_csv(run_id: int) -> tuple[str, str]:
    """(filename, csv text) for v1's "Export latest CSV"."""
    run = _row(
        "SELECT r.id, r.requested_at, q.keyword FROM keyword_runs r "
        "JOIN keyword_queries q ON q.id=r.query_id WHERE r.id=?",
        (int(run_id),),
    )
    if run is None:
        raise KeywordError(f"Run #{run_id} does not exist.")

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "rank", "page", "meta_page_id", "matching_ads", "represented",
        "total_active", "top_product", "oldest_matching_ad", "website_domain",
    ])
    for index, row in enumerate(run_results(run_id), start=1):
        writer.writerow([
            row.get("rank_position") or index, row["display_name"],
            row.get("platform_page_id") or "", _int(row.get("ads_matching")),
            _int(row.get("represented_matching")), _int(row.get("total_active_page_ads")),
            row.get("top_product") or "", row.get("oldest_matching_ad_date") or "",
            row.get("website_domain") or "",
        ])
    slug = normalize_name(run["keyword"]).replace(" ", "-") or "keyword"
    return f"keyword-run-{run_id}-{slug}.csv", buffer.getvalue()


def export_discovered_csv(query_id: int) -> tuple[str, str]:
    """The review list as CSV — the hand-off when the owner wants the pages
    somewhere else."""
    query = _row("SELECT keyword FROM keyword_queries WHERE id=?", (int(query_id),))
    if query is None:
        raise KeywordError(f"Saved search #{query_id} does not exist.")
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "page", "meta_page_id", "identity", "ads_seen", "times_seen", "ads_known",
        "active_known", "oldest_ad", "first_seen", "last_seen", "status", "tracked",
        "other_keywords", "ad_library_url",
    ])
    for row in discovered_pages(query_id):
        writer.writerow([
            row["display_name"], row["meta_page_id"], row["identity_kind"], _int(row["ads_seen"]),
            _int(row["times_seen"]), _int(row["ads_known"]), _int(row["active_known"]),
            row.get("oldest_ad") or "", row.get("first_seen_at") or "", row.get("last_seen_at") or "",
            row["review_status"], "yes" if row["is_tracked"] else "no",
            " | ".join(row["other_keywords"]), row["library_url"],
        ])
    slug = normalize_name(query["keyword"]).replace(" ", "-") or "keyword"
    return f"keyword-{query_id}-{slug}-discovered.csv", buffer.getvalue()


# ---------------------------------------------------------------------------
# website research (v1's domain panel)
# ---------------------------------------------------------------------------
def domain_matches(domain: str, limit: int = 60) -> list[dict]:
    """Advertiser pages already in the database whose products point at this
    domain. v1's note stands: an FB keyword run is a *text* search, so the
    fresh-from-Facebook half can never be an exact link match."""
    domain = looks_like_domain(domain)
    if not domain:
        return []
    like = f"%{domain}%"
    return _rows(
        """
        SELECT p.id AS page_id, COALESCE(p.alias, p.name) AS display_name,
               p.platform_page_id, p.active_ads,
               COUNT(DISTINCT a.id)  AS matching_ads,
               MIN(a.start_date)     AS oldest_ad
        FROM products pr
        JOIN ad_products ap ON ap.product_id = pr.id
        JOIN ads a          ON a.id = ap.ad_id
        JOIN pages p        ON p.id = a.page_id
        WHERE pr.domain LIKE ?
        GROUP BY p.id
        ORDER BY matching_ads DESC
        LIMIT ?
        """,
        (like, int(limit)),
    )


# ---------------------------------------------------------------------------
# one-time backfill from the v1 database
# ---------------------------------------------------------------------------
def backfill_from_v1(v1_path: str | Path) -> dict[str, int]:
    """Copy v1's keyword_queries / keyword_runs / keyword_results into v2.

    ``tools/import_v1.py`` stops at pages, ads, products, groups and metrics —
    it never carried the keyword tables, so without this the screen is an empty
    state on a database that has ten real saved searches behind it. Idempotent:
    a query already present (same keyword+filters) is reused, and a run already
    present (same query + requested_at) is skipped.

    Mapping keys are the same ones ``tools/import_v1.py`` uses:
    ``advertiser_pages.platform_page_id`` -> ``pages.platform_page_id``.
    """
    source = sqlite3.connect(f"file:{Path(v1_path)}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    stats = {"queries": 0, "runs": 0, "results": 0, "results_skipped_no_page": 0}
    try:
        page_map = _v1_page_map(source)
        with db.transaction():
            for v1_query in source.execute("SELECT * FROM keyword_queries ORDER BY id"):
                query_id = _backfill_query(v1_query)
                stats["queries"] += 1
                if str(v1_query["monitor_frequency"] or "off") != "off":
                    db.execute(
                        "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?)"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (
                            MONITOR_SETTING.format(query_id=query_id),
                            str(v1_query["monitor_frequency"]), utc_now(),
                        ),
                    )
                for v1_run in source.execute(
                    "SELECT * FROM keyword_runs WHERE query_id=? ORDER BY id", (v1_query["id"],)
                ):
                    run_id = _backfill_run(query_id, v1_run)
                    if run_id is None:
                        continue
                    stats["runs"] += 1
                    for result in source.execute(
                        "SELECT * FROM keyword_results WHERE run_id=?", (v1_run["id"],)
                    ):
                        page_id = page_map.get(int(result["page_id"]))
                        if page_id is None:
                            stats["results_skipped_no_page"] += 1
                            continue
                        db.execute(
                            """
                            INSERT OR IGNORE INTO keyword_results(
                                run_id, page_id, ads_matching, represented_matching,
                                total_active_page_ads, top_product, oldest_matching_ad_date,
                                rank_position, first_result_position,
                                selected_for_analysis, analysis_status)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                run_id, page_id,
                                _int(result["ads_matching_keyword"]),
                                _int(result["represented_ads_matching_keyword"]),
                                _int(result["total_active_page_ads"]),
                                result["top_product"], result["oldest_matching_ad_date"],
                                result["rank_position"], result["first_result_position"],
                                _int(result["selected_for_analysis"]),
                                str(result["analysis_status"] or "not_selected"),
                            ),
                        )
                        stats["results"] += 1
    finally:
        source.close()
    return stats


def _v1_page_map(source: sqlite3.Connection) -> dict[int, int]:
    v2_pages = {
        str(row["platform_page_id"]): int(row["id"])
        for row in db.fetch_all("SELECT id, platform_page_id FROM pages")
    }
    return {
        int(row["id"]): v2_pages[str(row["platform_page_id"])]
        for row in source.execute("SELECT id, platform_page_id FROM advertiser_pages")
        if str(row["platform_page_id"]) in v2_pages
    }


def _backfill_query(v1_query: sqlite3.Row) -> int:
    keyword = str(v1_query["keyword"] or "").strip()[:200]
    key = (
        keyword, str(v1_query["country"] or "IN"), str(v1_query["ad_status"] or "active"),
        str(v1_query["platform"] or "all"), str(v1_query["media_type"] or "all"),
    )
    created = str(v1_query["created_at"] or utc_now())
    db.execute(
        """
        INSERT INTO keyword_queries(keyword, country, ad_status, platform, media_type,
                                    default_depth, max_pages, sort_mode, is_saved,
                                    is_favorite, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(keyword, country, ad_status, platform, media_type) DO NOTHING
        """,
        (
            *key, _int(v1_query["default_depth"], DEPTH_DEFAULT),
            _int(v1_query["max_pages"], MAX_PAGES_DEFAULT),
            str(v1_query["sort_mode"] or "relevance"),
            _int(v1_query["is_saved"]), _int(v1_query["is_saved"]), created, created,
        ),
    )
    row = db.fetch_one(
        "SELECT id FROM keyword_queries WHERE keyword=? AND country=? AND ad_status=?"
        " AND platform=? AND media_type=?",
        key,
    )
    return int(row["id"])


def _backfill_run(query_id: int, v1_run: sqlite3.Row) -> int | None:
    requested_at = str(v1_run["requested_at"] or v1_run["started_at"] or utc_now())
    existing = db.fetch_one(
        "SELECT id FROM keyword_runs WHERE query_id=? AND requested_at=?",
        (query_id, requested_at),
    )
    if existing is not None:
        return None
    # An imported run that v1 left mid-flight has no v2 job behind it and no
    # worker that could ever finish it, so it is history, not queue. Importing
    # it as-is would drop a dozen un-actionable rows into the Research queue —
    # v1's own queue table was empty.
    status = str(v1_run["status"] or "completed").lower()
    if status not in {"completed", "failed", "cancelled"}:
        status = "cancelled"
    stop_reason = v1_run["stop_reason"] or (
        "imported from v1 while still open - the worker that owned it is gone"
        if str(v1_run["status"] or "").lower() in {"pending", "running"} else None
    )
    cursor = db.execute(
        """
        INSERT INTO keyword_runs(query_id, job_id, status, ads_scanned, unique_library_ids,
                                 represented_ads, unique_pages, scroll_count, duplicate_count,
                                 error_count, duration_seconds, stop_reason,
                                 requested_at, started_at, finished_at)
        VALUES(?, NULL, ?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            query_id, status,
            _int(v1_run["ads_scanned"]), _int(v1_run["unique_library_ids"]),
            _int(v1_run["represented_ads"]), _int(v1_run["unique_pages"]),
            _int(v1_run["scroll_count"]), _int(v1_run["duplicate_count"]),
            _int(v1_run["error_count"]), float(v1_run["duration_seconds"] or 0),
            stop_reason, requested_at, v1_run["started_at"], v1_run["finished_at"],
        ),
    )
    db.execute(
        "UPDATE keyword_queries SET last_run_at=MAX(COALESCE(last_run_at,''), ?) WHERE id=?",
        (requested_at, query_id),
    )
    return int(cursor.lastrowid)


__all__ = [
    "AD_STATUSES", "COUNTRIES", "MEDIA_TYPES", "MONITOR_CHOICES", "PLATFORMS",
    "REVIEW_ACTIONS", "REVIEW_STATUSES",
    "KeywordError", "advance_chains", "auto_scan_min_ads_default", "backfill_from_v1",
    "automation_from_form", "block_page", "blocked_results", "cumulative_results", "discovered_counts",
    "discovered_pages", "domain_matches", "enqueue", "export_discovered_csv",
    "export_run_csv", "filters_from_form", "get_automation", "get_monitor", "get_query",
    "keyword_search_url", "list_queries", "looks_like_domain", "parse_keyword",
    "query_detail", "queue_items", "rebuild_run_results", "remove_queue_item",
    "research_now", "review_page", "run_history", "run_results", "scan_all",
    "search_url_caps", "set_auto_scan", "set_auto_scan_min_ads_default", "set_favorite",
    "set_monitor", "split_bulk", "stage2_candidates", "stage2_scan", "start_run",
    "sync_runs_from_jobs", "unblock_page", "upsert_query",
]
