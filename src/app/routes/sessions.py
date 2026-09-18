"""Sessions — the extraction boundary and the arrival log against it.

v1's Sessions tab answers one question: *"is data still coming in, and how
much has landed since I pressed Start?"* A session is a flat storage boundary
— no nested runs, no batch browsing (v1's own words) — so the only thing this
screen writes is the boundary itself.

WHERE THE NUMBERS COME FROM. v2 already records every accepted upload in
``job_batches`` (batch id, page, ads seen/new/updated, represented count,
received_at) and every scrape in ``jobs``/``job_targets``. That *is* the
arrival log, so a live session derives its activity from those tables over its
own time window rather than keeping a second copy. 002's ``session_pages`` /
``session_ads`` / ``session_events`` are only read for sessions imported from
v1, whose batches predate v2's job tables and therefore cannot be derived from
anything. Per session: stored rows if it has any, otherwise derived. Two
sources, never for the same session, and only one of them is ever written to
by new work.

That is also the only design available: ``app/ingest.py`` is the shared write
path and does not know about sessions. Nothing here hooks it.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from flask import Blueprint, flash, redirect, render_template, request, url_for

from .. import db
from ..time_utils import age_seconds, utc_now

log = logging.getLogger("adspy2.sessions")

bp = Blueprint("sessions", __name__)

EVENT_LIMIT = 40
PAGE_LIMIT = 14
HISTORY_LIMIT = 30
RECENT_WINDOW_SECONDS = 15 * 60      # v1's "touched in 15m" figures


@bp.record_once
def _ensure_session_key(state) -> None:
    """Same guard as pages.py / queue.py: flash() needs a signed cookie and
    this blueprint has to work even if those modules fail to import."""
    app = state.app
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = (
            os.environ.get("ADSPY2_SECRET_KEY") or secrets.token_hex(32)
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _rows(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(row) for row in db.fetch_all(sql, params)]


def _row(sql: str, params: Iterable[Any] = ()) -> dict | None:
    row = db.fetch_one(sql, params)
    return dict(row) if row is not None else None


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def activity_state(last_activity_at: Any) -> tuple[str, str]:
    """v1's live pill: (text, class). 'live' green, 'warm' amber, 'idle' grey."""
    seconds = age_seconds(last_activity_at) if last_activity_at else None
    if seconds is None:
        return "No data yet", "idle"
    seconds = max(0, int(seconds))
    if seconds < 15:
        return "Receiving now", "live"
    if seconds < 60:
        return f"{seconds}s ago", "live"
    if seconds < 3600:
        return f"{seconds // 60}m ago", "live" if seconds < 900 else "warm"
    if seconds < 86400:
        return f"{seconds // 3600}h ago", "idle"
    return f"{seconds // 86400}d ago", "idle"


def _window(session: dict) -> tuple[str, str]:
    """(from, to) for deriving a session's activity out of the job tables.
    An open session runs to 'now', which sorts after every stored timestamp."""
    return str(session.get("started_at") or ""), str(session.get("completed_at") or "9999-12-31T23:59:59+00:00")


def _has_stored_rows(session_id: int) -> bool:
    return _row(
        "SELECT 1 AS present FROM session_events WHERE session_id=? LIMIT 1", (int(session_id),)
    ) is not None


# ---------------------------------------------------------------------------
# reads — imported sessions
# ---------------------------------------------------------------------------
def _stored_counters(session_id: int) -> dict:
    counts = _row(
        """
        SELECT (SELECT COUNT(*) FROM session_pages  WHERE session_id=?)  AS pages_stored,
               (SELECT COUNT(*) FROM session_ads    WHERE session_id=?)  AS ads_stored,
               (SELECT COALESCE(SUM(represented_ads), 0) FROM session_pages
                 WHERE session_id=?)                                     AS represented_ads,
               (SELECT COUNT(*) FROM session_events WHERE session_id=?)  AS ingest_events
        """,
        (int(session_id),) * 4,
    ) or {}
    # 002's session_events drops v1's page_ids_json, so "pages touched in 15m"
    # is the sum of the per-batch page counts rather than a distinct count.
    recent = _row(
        """
        SELECT COALESCE(SUM(page_count), 0) AS pages, COALESCE(SUM(ads_processed), 0) AS ads
        FROM session_events WHERE session_id = ? AND created_at >= ?
        """,
        (int(session_id), _recent_cutoff()),
    ) or {}
    counts["pages_last_15m"] = _int(recent.get("pages"))
    counts["ads_last_15m"] = _int(recent.get("ads"))
    return counts


def _stored_events(session_id: int, limit: int = EVENT_LIMIT) -> list[dict]:
    return _rows(
        """
        SELECT batch_id, source, keyword, page_count, ads_received, ads_processed,
               represented_ads, snapshot_complete, page_summary, created_at
        FROM session_events WHERE session_id=? ORDER BY id DESC LIMIT ?
        """,
        (int(session_id), int(limit)),
    )


def _stored_pages(session_id: int, limit: int = PAGE_LIMIT) -> list[dict]:
    return _rows(
        """
        SELECT sp.page_id, COALESCE(p.alias, p.name, p.platform_page_id) AS display_name,
               p.platform_page_id, sp.last_seen_at, sp.scrape_count,
               sp.active_ads AS live_ads, sp.represented_ads
        FROM session_pages sp JOIN pages p ON p.id = sp.page_id
        WHERE sp.session_id = ? ORDER BY sp.last_seen_at DESC LIMIT ?
        """,
        (int(session_id), int(limit)),
    )


# ---------------------------------------------------------------------------
# reads — live sessions, derived from jobs/job_batches
# ---------------------------------------------------------------------------
def _recent_cutoff() -> str:
    from ..time_utils import utc_shift

    return utc_shift(-RECENT_WINDOW_SECONDS)


def _derived_counters(session: dict) -> dict:
    start, end = _window(session)
    batches = _row(
        """
        SELECT COUNT(*)                                   AS ingest_events,
               COUNT(DISTINCT page_id)                    AS pages_stored,
               COALESCE(SUM(represented_ad_count), 0)     AS represented_ads
        FROM job_batches
        WHERE status='accepted' AND received_at >= ? AND received_at <= ?
        """,
        (start, end),
    ) or {}
    ads = _row(
        "SELECT COUNT(*) AS n FROM ads WHERE last_captured_at >= ? AND last_captured_at <= ?",
        (start, end),
    ) or {}
    cutoff = _recent_cutoff()
    recent = _row(
        """
        SELECT COUNT(DISTINCT page_id) AS pages, COALESCE(SUM(ads_new + ads_updated), 0) AS ads
        FROM job_batches
        WHERE status='accepted' AND received_at >= ? AND received_at >= ? AND received_at <= ?
        """,
        (start, cutoff, end),
    ) or {}
    return {
        "pages_stored": _int(batches.get("pages_stored")),
        "ads_stored": _int(ads.get("n")),
        "represented_ads": _int(batches.get("represented_ads")),
        "ingest_events": _int(batches.get("ingest_events")),
        "pages_last_15m": _int(recent.get("pages")),
        "ads_last_15m": _int(recent.get("ads")),
    }


def _derived_events(session: dict, limit: int = EVENT_LIMIT) -> list[dict]:
    start, end = _window(session)
    return _rows(
        """
        SELECT b.batch_id, 'extension' AS source, j.job_type AS job_type,
               COALESCE(t.label, j.label, '') AS keyword,
               COALESCE(p.alias, p.name, t.label, j.label, 'Accepted server ingest') AS page_summary,
               CASE WHEN b.page_id IS NULL THEN 0 ELSE 1 END AS page_count,
               b.ads_seen AS ads_received, (b.ads_new + b.ads_updated) AS ads_processed,
               b.represented_ad_count AS represented_ads,
               b.is_final AS snapshot_complete, b.received_at AS created_at
        FROM job_batches b
        JOIN jobs j ON j.id = b.job_id
        LEFT JOIN pages p ON p.id = b.page_id
        LEFT JOIN job_targets t ON t.job_id = b.job_id AND t.position = b.target_position
        WHERE b.status='accepted' AND b.received_at >= ? AND b.received_at <= ?
        ORDER BY b.id DESC LIMIT ?
        """,
        (start, end, int(limit)),
    )


def _derived_pages(session: dict, limit: int = PAGE_LIMIT) -> list[dict]:
    start, end = _window(session)
    return _rows(
        """
        SELECT p.id AS page_id, COALESCE(p.alias, p.name, p.platform_page_id) AS display_name,
               p.platform_page_id, MAX(b.received_at) AS last_seen_at,
               COUNT(*) AS scrape_count, p.active_ads AS live_ads,
               p.represented_ads AS represented_ads
        FROM job_batches b JOIN pages p ON p.id = b.page_id
        WHERE b.status='accepted' AND b.received_at >= ? AND b.received_at <= ?
        GROUP BY p.id ORDER BY last_seen_at DESC LIMIT ?
        """,
        (start, end, int(limit)),
    )


# ---------------------------------------------------------------------------
# reads — the dashboard
# ---------------------------------------------------------------------------
def _decorate(session: dict) -> dict:
    """Attach the counters, whichever source this session's data lives in."""
    session = dict(session)
    session["stored"] = _has_stored_rows(session["id"])
    counters = (
        _stored_counters(session["id"]) if session["stored"] else _derived_counters(session)
    )
    session.update(counters)
    # An imported session records its own last_activity_at; a derived one is
    # only as fresh as the newest batch inside its window.
    if not session["stored"]:
        start, end = _window(session)
        newest = _row(
            "SELECT MAX(received_at) AS at FROM job_batches "
            "WHERE status='accepted' AND received_at >= ? AND received_at <= ?",
            (start, end),
        ) or {}
        session["last_activity_at"] = newest.get("at") or session.get("last_activity_at")
    text, tone = activity_state(session.get("last_activity_at"))
    session["activity_text"] = text
    session["activity_tone"] = tone
    return session


def active_session() -> dict | None:
    row = _row("SELECT * FROM sessions WHERE status='active' ORDER BY session_number DESC LIMIT 1")
    return _decorate(row) if row else None


def get_session(session_id: int) -> dict | None:
    row = _row("SELECT * FROM sessions WHERE id=?", (int(session_id),))
    return _decorate(row) if row else None


def session_history(limit: int = HISTORY_LIMIT) -> list[dict]:
    rows = _rows(
        "SELECT * FROM sessions ORDER BY session_number DESC LIMIT ?", (int(limit),)
    )
    return [_decorate(row) for row in rows]


def load_dashboard(session_id: int | None = None) -> dict:
    """Everything the screen (and its 5s live fragment) paints."""
    active = active_session()
    display = get_session(session_id) if session_id else (active or _latest_session())
    events: list[dict] = []
    pages: list[dict] = []
    if display:
        if display["stored"]:
            events = _stored_events(display["id"])
            pages = _stored_pages(display["id"])
        else:
            events = _derived_events(display)
            pages = _derived_pages(display)
    for event in events:
        event["search_type"] = "keyword unordered" if _is_keyword_event(event) else "page"
    return {
        "active": active,
        "display": display,
        "sessions": session_history(),
        "events": events,
        "pages": pages,
        "server_time": utc_now(),
        "next_number": (_int(_latest_number()) + 1) or 1,
    }


def _is_keyword_event(event: dict) -> bool:
    if "job_type" in event:
        return str(event.get("job_type") or "") == "keyword"
    return bool(str(event.get("keyword") or "").strip())


def _latest_session() -> dict | None:
    row = _row("SELECT * FROM sessions ORDER BY session_number DESC LIMIT 1")
    return _decorate(row) if row else None


def _latest_number() -> int:
    row = _row("SELECT MAX(session_number) AS n FROM sessions")
    return _int((row or {}).get("n"))


# ---------------------------------------------------------------------------
# writes — the boundary, and nothing else
# ---------------------------------------------------------------------------
def start_session(keyword: str = "", notes: str = "") -> dict:
    """Complete whatever is active and open the next boundary.

    v1's contract, kept verbatim: starting session N+1 completes session N, and
    canonical ads are never duplicated because a session stores no ads — it
    only marks a window in time.
    """
    now = utc_now()
    with db.transaction():
        db.execute(
            "UPDATE sessions SET status='complete', completed_at=? WHERE status='active'",
            (now,),
        )
        number = _latest_number() + 1
        cursor = db.execute(
            """
            INSERT INTO sessions(session_number, name, keyword, notes, status,
                                 pages_seen, ads_seen, represented_ads,
                                 started_at, last_activity_at, completed_at, created_at)
            VALUES(?, ?, ?, ?, 'active', 0, 0, 0, ?, NULL, NULL, ?)
            """,
            (number, f"Session {number}", str(keyword or "")[:240], str(notes or "")[:1000], now, now),
        )
    return get_session(int(cursor.lastrowid)) or {}


def complete_active_session() -> dict | None:
    session = active_session()
    if session is None:
        return None
    now = utc_now()
    with db.transaction():
        db.execute(
            "UPDATE sessions SET status='complete', completed_at=?, "
            "pages_seen=?, ads_seen=?, represented_ads=? WHERE id=?",
            (
                now, _int(session.get("pages_stored")), _int(session.get("ads_stored")),
                _int(session.get("represented_ads")), int(session["id"]),
            ),
        )
    return get_session(int(session["id"]))


# ---------------------------------------------------------------------------
# screens
# ---------------------------------------------------------------------------
@bp.get("/sessions")
@bp.get("/session")
def index():
    session_id = request.args.get("session", type=int)
    data = load_dashboard(session_id)
    return render_template(
        "sessions.html",
        title="Sessions",
        active_nav="session",
        **data,
    )


@bp.get("/ui/sessions/live")
def live_fragment():
    """The 5s poll. Same macros as the first paint, so the live view can never
    drift from it — and no session markup is ever built outside Jinja."""
    session_id = request.args.get("session", type=int)
    data = load_dashboard(session_id)
    return render_template("sessions.html", fragment="live", **data)


@bp.post("/sessions/start")
def start():
    created = start_session(request.form.get("keyword", ""), request.form.get("notes", ""))
    flash(f"{created.get('name', 'Session')} is active.", "success")
    return redirect(request.form.get("next") or url_for("sessions.index"))


@bp.post("/sessions/complete")
def complete():
    completed = complete_active_session()
    if completed is None:
        flash("There is no active session to complete.", "warning")
    else:
        flash(f"{completed['name']} completed.", "success")
    return redirect(request.form.get("next") or url_for("sessions.index"))


# ---------------------------------------------------------------------------
# one-time backfill from the v1 database
# ---------------------------------------------------------------------------
def backfill_from_v1(v1_path: str | Path) -> dict[str, int]:
    """Copy v1's extraction_sessions + session_* into v2.

    ``tools/import_v1.py`` carries pages, ads, products, groups and metrics and
    stops there, so without this the screen shows an empty state on a database
    that has two real sessions and 1,959 real ingest receipts behind it. These
    rows are exactly the "what jobs cannot express" case the brief allows: they
    predate v2's job tables, so nothing can derive them.

    Idempotent on ``sessions.session_number``.
    """
    source = sqlite3.connect(f"file:{Path(v1_path)}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    stats = {"sessions": 0, "pages": 0, "ads": 0, "events": 0, "skipped_unmapped": 0}
    try:
        page_map, ad_map = _v1_maps(source)
        with db.transaction():
            for v1_session in source.execute("SELECT * FROM extraction_sessions ORDER BY session_number"):
                number = _int(v1_session["session_number"])
                existing = _row("SELECT id FROM sessions WHERE session_number=?", (number,))
                if existing is not None:
                    continue
                cursor = db.execute(
                    """
                    INSERT INTO sessions(session_number, name, keyword, notes, status,
                                         pages_seen, ads_seen, represented_ads,
                                         started_at, last_activity_at, completed_at, created_at)
                    VALUES(?,?,?,?,?,0,0,0,?,?,?,?)
                    """,
                    (
                        number, str(v1_session["name"] or f"Session {number}"),
                        str(v1_session["keyword"] or ""), str(v1_session["notes"] or ""),
                        "active" if str(v1_session["status"]) == "active" else "complete",
                        str(v1_session["started_at"] or v1_session["created_at"]),
                        v1_session["last_activity_at"], v1_session["completed_at"],
                        str(v1_session["created_at"] or utc_now()),
                    ),
                )
                session_id = int(cursor.lastrowid)
                stats["sessions"] += 1
                stats["pages"] += _copy_session_pages(source, v1_session["id"], session_id, page_map, stats)
                stats["ads"] += _copy_session_ads(source, v1_session["id"], session_id, page_map, ad_map, stats)
                stats["events"] += _copy_session_events(source, v1_session["id"], session_id)
                db.execute(
                    """
                    UPDATE sessions SET
                        pages_seen = (SELECT COUNT(*) FROM session_pages WHERE session_id=?),
                        ads_seen   = (SELECT COUNT(*) FROM session_ads   WHERE session_id=?),
                        represented_ads = (SELECT COALESCE(SUM(represented_ads), 0)
                                             FROM session_pages WHERE session_id=?)
                    WHERE id = ?
                    """,
                    (session_id,) * 4,
                )
    finally:
        source.close()
    return stats


def _v1_maps(source: sqlite3.Connection) -> tuple[dict[int, int], dict[int, int]]:
    """v1 row id -> v2 row id, on the same keys tools/import_v1.py used."""
    v2_pages = {
        str(row["platform_page_id"]): int(row["id"])
        for row in db.fetch_all("SELECT id, platform_page_id FROM pages")
    }
    v2_ads = {
        str(row["library_id"]): int(row["id"])
        for row in db.fetch_all("SELECT id, library_id FROM ads")
    }
    page_map = {
        int(row["id"]): v2_pages[str(row["platform_page_id"])]
        for row in source.execute("SELECT id, platform_page_id FROM advertiser_pages")
        if str(row["platform_page_id"]) in v2_pages
    }
    ad_map = {
        int(row["id"]): v2_ads[str(row["library_id"])]
        for row in source.execute("SELECT id, library_id FROM ads")
        if str(row["library_id"]) in v2_ads
    }
    return page_map, ad_map


def _copy_session_pages(source, v1_session_id, session_id, page_map, stats) -> int:
    copied = 0
    for row in source.execute("SELECT * FROM session_pages WHERE session_id=?", (v1_session_id,)):
        page_id = page_map.get(int(row["page_id"]))
        if page_id is None:
            stats["skipped_unmapped"] += 1
            continue
        db.execute(
            """
            INSERT OR IGNORE INTO session_pages(session_id, page_id, first_seen_at, last_seen_at,
                                                scrape_count, active_ads, represented_ads, meta_results)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                session_id, page_id, row["first_seen_at"], row["last_seen_at"],
                _int(row["scrape_count"], 1), _int(row["live_ads"]),
                _int(row["represented_ads"]), row["meta_result_count"],
            ),
        )
        copied += 1
    return copied


def _copy_session_ads(source, v1_session_id, session_id, page_map, ad_map, stats) -> int:
    copied = 0
    for row in source.execute("SELECT * FROM session_ads WHERE session_id=?", (v1_session_id,)):
        ad_id = ad_map.get(int(row["ad_id"]))
        if ad_id is None:
            stats["skipped_unmapped"] += 1
            continue
        db.execute(
            """
            INSERT OR IGNORE INTO session_ads(session_id, ad_id, page_id, first_seen_at,
                                              last_seen_at, seen_count)
            VALUES(?,?,?,?,?,?)
            """,
            (
                session_id, ad_id,
                page_map.get(_int(row["page_id"])) if row["page_id"] else None,
                row["first_seen_at"], row["last_seen_at"], _int(row["seen_count"], 1),
            ),
        )
        copied += 1
    return copied


def _copy_session_events(source, v1_session_id, session_id) -> int:
    copied = 0
    for row in source.execute(
        "SELECT * FROM session_ingest_events WHERE session_id=? ORDER BY id", (v1_session_id,)
    ):
        db.execute(
            """
            INSERT INTO session_events(session_id, batch_id, source, keyword, page_count,
                                       ads_received, ads_processed, represented_ads,
                                       snapshot_complete, page_summary, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session_id, row["batch_id"], str(row["source"] or "extension"),
                str(row["keyword"] or ""), _int(row["page_count"]),
                _int(row["ads_received"]), _int(row["ads_processed"]),
                _int(row["represented_ads"]), _int(row["snapshot_complete"]),
                str(row["page_summary"] or ""), str(row["created_at"] or utc_now()),
            ),
        )
        copied += 1
    return copied


__all__ = [
    "activity_state", "active_session", "backfill_from_v1", "bp",
    "complete_active_session", "get_session", "load_dashboard",
    "session_history", "start_session",
]
