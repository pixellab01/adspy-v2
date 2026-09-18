"""Diagnostics — the durable issue log behind the Logs screen.

WHY THIS EXISTS
---------------
The owner's ask, in his own words: *"if any bug or anything comes up, one click
and it is saved to the logs, and whenever we restart, the logs tell me which
bugs happened and what still needs solving."*

Two words in that sentence decide the whole design. **Restart** — so a ring
buffer in memory or in ``chrome.storage`` is not an answer. **Still needs
solving** — so a chronological text dump is not an answer either: a dump grows,
repeats itself and never says which line is still true. What answers it is an
issue tracker with a resolve button:

    one row per DISTINCT problem, a count of how often it recurred,
    a Resolve that stops it nagging, and a reopen the next time it happens.

HOW A PROBLEM GETS IN HERE — three doors, no daemon
---------------------------------------------------
1. **Unhandled server exceptions.** ``install_error_capture(app)`` connects to
   Flask's ``got_request_exception`` signal (app/routes/logs.py calls it from
   ``record_once``, so nothing else in the app has to know). The write goes on
   its own short-lived connection, because the request's own transaction may
   already be rolling back underneath us.

2. **A sweep over facts the database already holds** (:func:`sweep`) — failed /
   partial / blocked scrape targets, failed jobs, rejected batches, pages stuck
   in ``current_scan_status='error'``. It runs when the Logs screen opens, at
   most once a minute, and walks forward from a per-source watermark so a fact
   is never counted twice. This is the same "derive it in the request" trick
   app/routes/alerts.py uses instead of v1's sweep thread.

3. **The extension**, via ``POST /api/logs/worker`` (:func:`record_worker_lines`):
   its 200-line ring buffer plus any halt reason. Block signals, captchas and
   login walls become issues; everything else is kept as searchable log lines.

DEDUPE — the anti-nag mechanism
-------------------------------
``issue_log.fingerprint`` is ``sha1(kind | dedupe-key | message with every digit
collapsed to '#')``. "Frame with ID 0 is showing error page" on page 324 is ONE
row whether it happened once or forty times, and whether it happened under job
4 or job 11. ``occurrences`` carries the count; ``location`` shows the most
recent sighting. A recurrence after a Resolve reopens the row and bumps
``reopened_count`` — "I fixed it" is a claim the next occurrence disproves.

CHEAP, AS INSTRUCTED
--------------------
No thread, no log shipping, no tailing daemon. Bounded retention on all three
tables (:data:`ISSUE_LIMIT`, :data:`WORKER_LINE_LIMIT`, :data:`SNAPSHOT_LIMIT`),
enforced on write. The gunicorn logs are read only when a snapshot is built or
the screen is open, and only their tail.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import traceback
from pathlib import Path
from typing import Any, Iterable

from . import config as app_config
from . import db
from .time_utils import age_seconds, utc_now

log = logging.getLogger("adspy2.logs")


# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------
SERVER_ERROR = "server_error"
SCAN_FAILED = "scan_failed"
SCAN_PARTIAL = "scan_partial"
JOB_FAILED = "job_failed"
BATCH_REJECTED = "batch_rejected"
PAGE_ERROR = "page_error"
WORKER_BLOCK = "worker_block"
WORKER_ERROR = "worker_error"
MANUAL = "manual"

#: Display order on the screen's filter row — worst first.
KINDS: tuple[str, ...] = (
    SERVER_ERROR, WORKER_BLOCK, SCAN_FAILED, JOB_FAILED,
    BATCH_REJECTED, PAGE_ERROR, SCAN_PARTIAL, WORKER_ERROR, MANUAL,
)

KIND_LABELS: dict[str, str] = {
    SERVER_ERROR: "Server error",
    WORKER_BLOCK: "Extension blocked",
    SCAN_FAILED: "Scan failed",
    JOB_FAILED: "Job failed",
    BATCH_REJECTED: "Batch rejected",
    PAGE_ERROR: "Page stuck in error",
    SCAN_PARTIAL: "Scan incomplete",
    WORKER_ERROR: "Extension error",
    MANUAL: "Noted by hand",
}

SEVERITIES: tuple[str, ...] = ("info", "warn", "error")
SOURCES: tuple[str, ...] = ("server", "extension", "manual")

STATE_OPEN = "open"
STATE_RESOLVED = "resolved"
STATE_ALL = "all"
STATES: tuple[str, ...] = (STATE_OPEN, STATE_RESOLVED, STATE_ALL)

# --- retention (bounded, enforced on write; there is no cleanup job) ---------
ISSUE_LIMIT = 2000            # rows in issue_log; resolved rows are shed first
WORKER_LINE_LIMIT = 1000      # rows in worker_log_lines
SNAPSHOT_LIMIT = 20           # files under logs/snapshots/, oldest deleted
SWEEP_COOLDOWN_SECONDS = 60   # how often opening /logs may re-derive issues
FEED_LIMIT = 300              # rows the screen renders
DETAIL_MAX = 4000             # characters kept of a traceback / raw message
TITLE_MAX = 240

SWEEP_AT_KEY = "logs.last_sweep_at"
CURSOR_PREFIX = "logs.cursor."


class LogError(Exception):
    """Something the caller asked for is not there (bad id, missing file)."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clip(value: Any, limit: int) -> str:
    text = _text(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


_DIGITS = re.compile(r"\d+")
_SPACES = re.compile(r"\s+")


def normalise(text: Any) -> str:
    """The message with the varying parts taken out: lowercase, every digit run
    collapsed to '#', whitespace squeezed. Two sightings of the same bug differ
    only in ids, counts and timestamps — this is what makes them one row."""
    squeezed = _SPACES.sub(" ", _text(text).lower())
    squeezed = re.sub(r"0x[0-9a-f]+", "#", squeezed)
    return _DIGITS.sub("#", squeezed)[:400]


def fingerprint_for(kind: str, dedupe: str, message: str) -> str:
    """kind + dedupe key + normalised message, hashed.

    The dedupe key is NOT normalised, and that distinction is load-bearing: it
    is chosen by the caller precisely because it identifies the thing
    ("page_error:350759231463449"), and collapsing its digits would fold every
    page stuck in error into a single row. The *message* is the part that
    varies between sightings of the same bug, so that is the part that gets its
    digits taken out.
    """
    key = _SPACES.sub(" ", _text(dedupe).lower())[:200]
    raw = f"{kind}|{key}|{normalise(message)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def time_ago(value: Any) -> str:
    """'4 min ago' — the only time format this screen shows in a table cell."""
    seconds = age_seconds(_text(value) or None)
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours} hr{'' if hours == 1 else 's'} ago"
    days = seconds // 86400
    return f"{days} day{'' if days == 1 else 's'} ago"


def _db_file() -> str:
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            return str(current_app.config["DATABASE"])
    except ImportError:                                    # pragma: no cover
        pass
    return app_config.db_path()


def _config_path(key: str, default: Path) -> Path:
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            value = current_app.config.get(key)
            if value:
                return Path(str(value))
    except ImportError:                                    # pragma: no cover
        pass
    return default


def log_dir() -> Path:
    """Where gunicorn writes server.access.log / server.error.log (adspy2:30)."""
    return _config_path("LOG_DIR", app_config.LOG_DIR)


def snapshot_dir() -> Path:
    """Where "Save current logs" puts its files. Tests point this at tmp_path."""
    return _config_path("LOG_SNAPSHOT_DIR", app_config.LOG_DIR / "snapshots")


# ---------------------------------------------------------------------------
# settings-backed watermarks (the sweep's memory)
# ---------------------------------------------------------------------------
def _setting(key: str, default: str = "") -> str:
    row = db.fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
    return _text(row["value"]) if row else default


def _set_setting(key: str, value: str) -> None:
    db.execute(
        """
        INSERT INTO settings(key, value, updated_at) VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                       updated_at = excluded.updated_at
        """,
        (key, _text(value), utc_now()),
    )


def _cursor(name: str) -> str:
    return _setting(CURSOR_PREFIX + name)


def _set_cursor(name: str, value: Any) -> None:
    if _text(value):
        _set_setting(CURSOR_PREFIX + name, _text(value))


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------
def record_issue(
    kind: str,
    title: str,
    *,
    detail: str = "",
    location: str = "",
    dedupe: str | None = None,
    severity: str = "warn",
    source: str = "server",
    entity_type: str | None = None,
    entity_id: Any = None,
    job_id: Any = None,
    page_id: Any = None,
    seen_at: str | None = None,
    isolated: bool = False,
) -> dict[str, Any]:
    """Record one sighting of a problem. Returns ``{id, occurrences, created,
    reopened}``.

    ``dedupe`` is what decides whether this is the same problem as last time;
    it defaults to ``location``. Pass something *stable* — a page identity, a
    route pattern — never a job id or a timestamp, or the screen fills up with
    twenty rows saying the same thing.

    ``isolated=True`` writes on its own connection. The exception hook needs
    that: the request's transaction may be mid-rollback, and a diagnostics
    write must never be the reason a 500 becomes a crash.
    """
    kind = _text(kind) or MANUAL
    severity = severity if severity in SEVERITIES else "warn"
    source = source if source in SOURCES else "server"
    title = _clip(title, TITLE_MAX) or KIND_LABELS.get(kind, kind)
    detail = _clip(detail, DETAIL_MAX)
    location = _clip(location, 240)
    now = _text(seen_at) or utc_now()
    finger = fingerprint_for(kind, dedupe if dedupe is not None else location, title)

    row = (
        finger, kind, severity, source, title, detail, location,
        _text(entity_type) or None, _int_or_none(entity_id),
        _int_or_none(job_id), _int_or_none(page_id), now, now,
    )

    if isolated:
        conn = db.connect(_db_file())
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = _upsert_issue(conn, row)
            conn.execute("COMMIT")
        finally:
            conn.close()
        return result

    with db.transaction():
        return _upsert_issue(db.get_db(), row)


def _upsert_issue(conn: sqlite3.Connection, row: tuple) -> dict[str, Any]:
    """The one write. ON CONFLICT counts the recurrence instead of inserting.

    Every bare column name on the right of a SET refers to the row as it was
    BEFORE this statement, which is what makes the reopen bookkeeping work:
    ``resolved_at`` is read (was it closed?) in the same statement that clears
    it.
    """
    before = conn.execute(
        "SELECT id, occurrences, resolved_at FROM issue_log WHERE fingerprint = ?",
        (row[0],),
    ).fetchone()

    conn.execute(
        """
        INSERT INTO issue_log(
            fingerprint, kind, severity, source, title, detail, location,
            entity_type, entity_id, job_id, page_id, occurrences,
            reopened_count, first_seen_at, last_seen_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,0,?,?)
        ON CONFLICT(fingerprint) DO UPDATE SET
            occurrences    = occurrences + 1,
            last_seen_at   = excluded.last_seen_at,
            severity       = excluded.severity,
            title          = excluded.title,
            detail         = CASE WHEN excluded.detail = '' THEN detail
                                  ELSE excluded.detail END,
            location       = CASE WHEN excluded.location = '' THEN location
                                  ELSE excluded.location END,
            job_id         = COALESCE(excluded.job_id, job_id),
            page_id        = COALESCE(excluded.page_id, page_id),
            entity_type    = COALESCE(excluded.entity_type, entity_type),
            entity_id      = COALESCE(excluded.entity_id, entity_id),
            reopened_count = reopened_count
                             + CASE WHEN resolved_at IS NULL THEN 0 ELSE 1 END,
            resolved_at    = NULL,
            resolved_note  = CASE WHEN resolved_at IS NULL THEN resolved_note ELSE '' END
        """,
        row,
    )

    after = conn.execute(
        "SELECT id, occurrences FROM issue_log WHERE fingerprint = ?", (row[0],)
    ).fetchone()
    created = before is None
    if created:
        _trim_issues(conn)
    return {
        "id": int(after["id"]),
        "occurrences": int(after["occurrences"]),
        "created": created,
        "reopened": bool(before is not None and before["resolved_at"]),
    }


def _trim_issues(conn: sqlite3.Connection) -> int:
    """Bounded retention. Resolved rows are shed before open ones — an open
    issue is a to-do and must not be evicted by a burst of fixed ones."""
    total = int(conn.execute("SELECT COUNT(*) FROM issue_log").fetchone()[0])
    excess = total - ISSUE_LIMIT
    if excess <= 0:
        return 0
    conn.execute(
        """
        DELETE FROM issue_log WHERE id IN (
            SELECT id FROM issue_log
            ORDER BY (resolved_at IS NULL), last_seen_at, id
            LIMIT ?
        )
        """,
        (excess,),
    )
    return excess


def record_manual_note(title: str, detail: str = "") -> dict[str, Any]:
    """The owner typing "products page looked wrong after the 3pm scan"."""
    return record_issue(
        MANUAL, title, detail=detail, location="noted by hand",
        dedupe=f"manual:{normalise(title)}", severity="info", source="manual",
    )


# ---------------------------------------------------------------------------
# door 1 — unhandled server exceptions
# ---------------------------------------------------------------------------
def install_error_capture(app) -> bool:
    """Connect to ``got_request_exception`` so every 500 lands in issue_log.

    Flask's own 500 handler still runs and still renders the error page; this
    only listens. Returns False when blinker is unavailable, in which case the
    other two doors still work and the screen simply never shows server errors.
    """
    if app.config.get("_ADSPY_LOG_CAPTURE"):
        return True
    try:
        from flask import got_request_exception
    except ImportError:                                    # pragma: no cover
        return False
    got_request_exception.connect(_on_request_exception, app)
    app.config["_ADSPY_LOG_CAPTURE"] = True
    return True


def _on_request_exception(sender, exception: BaseException, **_extra) -> None:
    """Signal receiver. Must never raise: it runs while a request is already
    failing, and an exception here would replace the real error with this one."""
    try:
        from flask import request

        path = _text(request.path)
        route = _text(getattr(request.url_rule, "rule", "")) or path
        method = _text(request.method)
        capture_exception(exception, location=f"{method} {path}", dedupe_route=route)
    except Exception:                                      # pragma: no cover
        log.exception("diagnostics capture failed")


def capture_exception(
    exception: BaseException,
    *,
    location: str = "",
    dedupe_route: str = "",
    kind: str = SERVER_ERROR,
) -> dict[str, Any] | None:
    """Turn an exception into an issue. Fingerprinted on route + exception type
    + the deepest frame, so the same bug hit from three URLs with different ids
    is one row, and two different bugs on one route are two."""
    frames = traceback.extract_tb(exception.__traceback__)
    last = frames[-1] if frames else None
    where = f"{Path(last.filename).name}:{last.lineno}" if last else "?"
    title = f"{type(exception).__name__}: {exception}"
    detail = "".join(
        traceback.format_exception(type(exception), exception, exception.__traceback__)
    )
    try:
        return record_issue(
            kind,
            title,
            detail=detail[-DETAIL_MAX:],
            location=f"{location} ({where})".strip(),
            dedupe=f"{dedupe_route or location}|{type(exception).__name__}|{where}",
            severity="error",
            source="server",
            entity_type="route",
            isolated=True,
        )
    except sqlite3.Error:                                  # pragma: no cover
        log.exception("could not store server error")
        return None


# ---------------------------------------------------------------------------
# door 2 — the sweep over what the database already knows
# ---------------------------------------------------------------------------
def sweep(force: bool = False) -> dict[str, Any]:
    """Derive issues from jobs / job_targets / job_batches / pages.

    Cheap by construction: every source is a single indexed query walking
    forward from a watermark held in ``settings``, so a fact is examined once
    and only once no matter how often the screen is opened.
    """
    last = _setting(SWEEP_AT_KEY)
    if not force and last:
        seconds = age_seconds(last)
        if seconds is not None and seconds < SWEEP_COOLDOWN_SECONDS:
            return {"ran": False, "reason": "cooldown", "created": 0, "seen": 0}

    created = 0
    seen = 0
    for step in (_sweep_targets, _sweep_jobs, _sweep_batches, _sweep_pages):
        outcome = step()
        created += outcome["created"]
        seen += outcome["seen"]

    with db.transaction():
        _set_setting(SWEEP_AT_KEY, utc_now())
    return {"ran": True, "reason": "", "created": created, "seen": seen}


def _run_source(name: str, rows: Iterable[sqlite3.Row], handler) -> dict[str, int]:
    """Walk one source forward and remember where we stopped.

    Every query above orders ASCENDING by the very column it hands back as the
    watermark, so the LAST row seen is the high-water mark. Deliberately not
    ``max(mark, high)``: the batch cursor is an integer stored as text, and a
    string comparison would decide that "9" > "10" and re-sweep row 10 forever,
    inflating its occurrence count on every screen open.
    """
    created = 0
    seen = 0
    high = _cursor(name)
    for row in rows:
        result, mark = handler(row)
        seen += 1
        if result and result.get("created"):
            created += 1
        if _text(mark):
            high = _text(mark)
    if seen:
        with db.transaction():
            _set_cursor(name, high)
    return {"created": created, "seen": seen}


def _sweep_targets() -> dict[str, int]:
    """Failed / blocked / partial scrape targets — the owner's "which pages did
    not finish". ``partial`` is a warning, not an error: coverage graded it
    honestly and nothing was mass-deactivated, but the page is short."""
    since = _cursor("job_targets")
    rows = db.fetch_all(
        """
        SELECT t.*, j.job_type
          FROM job_targets t
          JOIN jobs j ON j.id = t.job_id
         WHERE t.finished_at IS NOT NULL
           AND t.finished_at > ?
           AND (t.status IN ('failed','skipped')
                OR t.outcome IN ('failed','blocked','partial'))
         ORDER BY t.finished_at
         LIMIT 500
        """,
        (since,),
    )

    def handle(row: sqlite3.Row):
        outcome = _text(row["outcome"]) or _text(row["status"])
        label = _text(row["label"]) or _text(row["platform_page_id"]) or "unnamed target"
        partial = outcome == "partial"
        message = _text(row["message"]) or outcome
        detail = (
            f"outcome={outcome} status={_text(row['status'])} "
            f"scrolls={row['scrolls']} unique_ads={row['unique_ads']} "
            f"represented={row['represented_ads']} estimate={row['estimated_results']}"
        )
        result = record_issue(
            SCAN_PARTIAL if partial else SCAN_FAILED,
            f"{label}: scan ended {outcome} ({message})",
            detail=detail,
            location=f"job {row['job_id']} - target {row['position']} - {label}",
            dedupe=f"target:{_text(row['platform_page_id']) or label}:{message}",
            severity="warn" if partial else "error",
            source="extension",
            entity_type="target",
            entity_id=row["id"],
            job_id=row["job_id"],
            page_id=row["page_id"],
            seen_at=_text(row["finished_at"]),
        )
        return result, row["finished_at"]

    return _run_source("job_targets", rows, handle)


def _sweep_jobs() -> dict[str, int]:
    """Jobs that ended badly. A job the OWNER cancelled is excluded: he already
    knows, and "Cancelled by the dashboard" sitting in a to-do list of bugs is
    exactly the noise that makes people stop reading the list."""
    since = _cursor("jobs")
    rows = db.fetch_all(
        """
        SELECT * FROM jobs
         WHERE finished_at IS NOT NULL AND finished_at > ?
           AND (status = 'failed' OR outcome = 'failed'
                OR (error IS NOT NULL AND error <> ''))
           AND status <> 'cancelled'
           AND COALESCE(error_code, '') <> 'JOB_CANCELLED'
         ORDER BY finished_at
         LIMIT 200
        """,
        (since,),
    )

    def handle(row: sqlite3.Row):
        code = _text(row["error_code"]) or "no code"
        message = _text(row["error"]) or _text(row["outcome"]) or "job failed"
        result = record_issue(
            JOB_FAILED,
            f"Job #{row['id']} failed: {message}",
            detail=(
                f"error_code={code} retryable={row['retryable']} "
                f"retries={row['retry_count']}/{row['max_retries']} "
                f"targets={row['targets_done']}/{row['targets_total']}"
            ),
            location=f"job {row['id']} - {_text(row['label']) or _text(row['job_type'])}",
            dedupe=f"job:{code}:{message}",
            severity="error",
            source="extension",
            entity_type="job",
            entity_id=row["id"],
            job_id=row["id"],
            seen_at=_text(row["finished_at"]),
        )
        return result, row["finished_at"]

    return _run_source("jobs", rows, handle)


def _sweep_batches() -> dict[str, int]:
    """Rejected batches: ads the extension sent and ingest refused. Silent
    today — the extension logs an error the dashboard never sees."""
    since = _int_or_none(_cursor("job_batches")) or 0
    rows = db.fetch_all(
        """
        SELECT * FROM job_batches
         WHERE status = 'rejected' AND id > ?
         ORDER BY id
         LIMIT 300
        """,
        (since,),
    )

    def handle(row: sqlite3.Row):
        reason = _text(row["outcome"]) or "rejected"
        result = record_issue(
            BATCH_REJECTED,
            f"Batch rejected on job #{row['job_id']}: {reason}",
            detail=(
                f"batch_id={_text(row['batch_id'])} target={row['target_position']} "
                f"ads_seen={row['ads_seen']} is_final={row['is_final']}"
            ),
            location=f"job {row['job_id']} - batch {_text(row['batch_id'])}",
            dedupe=f"batch:{reason}",
            severity="warn",
            source="extension",
            entity_type="batch",
            entity_id=row["id"],
            job_id=row["job_id"],
            page_id=row["page_id"],
            seen_at=_text(row["received_at"]),
        )
        return result, str(row["id"])

    return _run_source("job_batches", rows, handle)


def _sweep_pages() -> dict[str, int]:
    """A page red since 2026-08-04 with nothing able to clear it is exactly the
    thing that should be on a to-do list rather than in a status column."""
    since = _cursor("pages")
    rows = db.fetch_all(
        """
        SELECT id, name, alias, platform_page_id, updated_at, last_captured_at
          FROM pages
         WHERE current_scan_status = 'error' AND updated_at > ?
         ORDER BY updated_at
         LIMIT 300
        """,
        (since,),
    )

    def handle(row: sqlite3.Row):
        label = _text(row["alias"]) or _text(row["name"]) or f"Page {row['id']}"
        result = record_issue(
            PAGE_ERROR,
            f"{label} is stuck in scan status 'error'",
            detail=(
                f"platform_page_id={_text(row['platform_page_id'])} "
                f"last_captured_at={_text(row['last_captured_at']) or 'never'}"
            ),
            location=f"page {row['id']} - {label}",
            dedupe=f"page_error:{_text(row['platform_page_id']) or row['id']}",
            severity="warn",
            source="server",
            entity_type="page",
            entity_id=row["id"],
            page_id=row["id"],
            seen_at=_text(row["updated_at"]),
        )
        return result, row["updated_at"]

    return _run_source("pages", rows, handle)


# ---------------------------------------------------------------------------
# door 3 — the extension's ring buffer
# ---------------------------------------------------------------------------
BLOCK_WORDS = ("captcha", "login wall", "login_wall", "checkpoint", "blocked",
               "halt", "halted", "rate limit", "too many requests")


def record_worker_lines(payload: dict[str, Any]) -> dict[str, Any]:
    """Land ``POST /api/logs/worker``: bounded log lines + issues for the bad
    ones. Replay-safe — the extension re-sends its whole buffer every time and
    ``UNIQUE(worker_id, line_hash)`` makes the repeats free."""
    worker_id = _clip(payload.get("installationId") or payload.get("workerId"), 80)
    version = _clip(payload.get("extensionVersion"), 40)
    state = _clip(payload.get("state"), 40)
    halt_reason = _clip(payload.get("haltReason"), 300)
    raw_lines = payload.get("lines")
    lines = raw_lines if isinstance(raw_lines, list) else []

    now = utc_now()
    stored = 0
    issues = 0

    with db.transaction():
        conn = db.get_db()
        for entry in lines[-WORKER_LINE_LIMIT:]:
            if isinstance(entry, dict):
                level = _clip(entry.get("level") or entry.get("kind") or "info", 16).lower()
                logged_at = _clip(entry.get("t") or entry.get("at") or entry.get("time"), 40)
                message = _clip(entry.get("msg") or entry.get("message") or entry.get("text"), 1000)
            else:
                level, logged_at, message = "info", "", _clip(entry, 1000)
            if not message:
                continue
            line_hash = hashlib.sha1(
                f"{logged_at}|{level}|{message}".encode("utf-8")
            ).hexdigest()
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO worker_log_lines(
                    worker_id, level, logged_at, message, line_hash, received_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (worker_id, level or "info", logged_at, message, line_hash, now),
            )
            if cursor.rowcount:
                stored += 1
        _trim_worker_lines(conn)

    # Issues are recorded outside the loop's transaction: record_issue takes its
    # own, and nesting is a passthrough (app/db.py::transaction), so this is one
    # commit per issue rather than one giant write holding the lock.
    lowered_seen: set[str] = set()
    for entry in lines:
        if not isinstance(entry, dict):
            continue
        level = _text(entry.get("level") or entry.get("kind")).lower()
        message = _clip(entry.get("msg") or entry.get("message") or entry.get("text"), 1000)
        if not message:
            continue
        lowered = message.lower()
        blocking = any(word in lowered for word in BLOCK_WORDS)
        if level not in ("error", "fatal") and not blocking:
            continue
        key = normalise(message)
        if key in lowered_seen:
            continue
        lowered_seen.add(key)
        record_issue(
            WORKER_BLOCK if blocking else WORKER_ERROR,
            message,
            detail=f"worker={worker_id or 'unknown'} version={version} state={state}",
            location=f"extension {worker_id or 'unknown'}",
            dedupe=f"worker:{key}",
            severity="error",
            source="extension",
            entity_type="worker",
            seen_at=_clip(entry.get("t") or entry.get("at"), 40) or now,
        )
        issues += 1

    if halt_reason:
        record_issue(
            WORKER_BLOCK,
            f"Extension halted: {halt_reason}",
            detail=f"worker={worker_id or 'unknown'} version={version} state={state or 'halted'}",
            location=f"extension {worker_id or 'unknown'}",
            dedupe=f"worker-halt:{normalise(halt_reason)}",
            severity="error",
            source="extension",
            entity_type="worker",
        )
        issues += 1

    return {"stored": stored, "issues": issues, "workerId": worker_id}


def _trim_worker_lines(conn: sqlite3.Connection) -> int:
    total = int(conn.execute("SELECT COUNT(*) FROM worker_log_lines").fetchone()[0])
    excess = total - WORKER_LINE_LIMIT
    if excess <= 0:
        return 0
    conn.execute(
        "DELETE FROM worker_log_lines WHERE id IN "
        "(SELECT id FROM worker_log_lines ORDER BY id LIMIT ?)",
        (excess,),
    )
    return excess


def worker_lines(limit: int = 120) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        "SELECT * FROM worker_log_lines ORDER BY id DESC LIMIT ?", (int(limit),)
    )
    return [
        {
            "id": int(row["id"]),
            "worker_id": _text(row["worker_id"]),
            "level": _text(row["level"]),
            "logged_at": _text(row["logged_at"]) or _text(row["received_at"]),
            "message": _text(row["message"]),
            "received_at": _text(row["received_at"]),
        }
        for row in rows
    ]


def worker_summary() -> dict[str, Any]:
    """What the server knows about the extension. ``workers`` is written by the
    hello endpoint (a separate change); until it has rows this falls back to
    the newest log line, which is still better than the dashboard's current
    answer, which is nothing at all."""
    row = db.fetch_one(
        "SELECT worker_id, status, extension_version, last_error, last_heartbeat_at, "
        "last_seen_at FROM workers ORDER BY last_heartbeat_at DESC LIMIT 1"
    )
    line = db.fetch_one(
        "SELECT worker_id, received_at FROM worker_log_lines ORDER BY id DESC LIMIT 1"
    )
    last_seen = ""
    worker_id = ""
    status = ""
    version = ""
    error = ""
    if row is not None:
        worker_id = _text(row["worker_id"])
        status = _text(row["status"])
        version = _text(row["extension_version"])
        error = _text(row["last_error"])
        last_seen = _text(row["last_heartbeat_at"]) or _text(row["last_seen_at"])
    if line is not None and _text(line["received_at"]) > last_seen:
        last_seen = _text(line["received_at"])
        worker_id = worker_id or _text(line["worker_id"])
    return {
        "worker_id": worker_id,
        "status": status,
        "version": version,
        "last_error": error,
        "last_seen_at": last_seen,
        "last_seen": time_ago(last_seen) if last_seen else "never",
        "known": bool(last_seen),
    }


# ---------------------------------------------------------------------------
# reading the issue log
# ---------------------------------------------------------------------------
def _issue_dict(row: sqlite3.Row) -> dict[str, Any]:
    resolved = _text(row["resolved_at"])
    return {
        "id": int(row["id"]),
        "kind": _text(row["kind"]),
        "kind_label": KIND_LABELS.get(_text(row["kind"]), _text(row["kind"])),
        "severity": _text(row["severity"]),
        "source": _text(row["source"]),
        "title": _text(row["title"]),
        "detail": _text(row["detail"]),
        "location": _text(row["location"]),
        "entity_type": _text(row["entity_type"]),
        "entity_id": _int_or_none(row["entity_id"]),
        "job_id": _int_or_none(row["job_id"]),
        "page_id": _int_or_none(row["page_id"]),
        "occurrences": int(row["occurrences"]),
        "reopened_count": int(row["reopened_count"]),
        "first_seen_at": _text(row["first_seen_at"]),
        "last_seen_at": _text(row["last_seen_at"]),
        "first_seen": time_ago(row["first_seen_at"]),
        "last_seen": time_ago(row["last_seen_at"]),
        "resolved_at": resolved,
        "resolved_note": _text(row["resolved_note"]),
        "is_open": not resolved,
    }


def list_issues(
    state: str = STATE_OPEN,
    kind: str = "",
    limit: int = FEED_LIMIT,
) -> list[dict[str, Any]]:
    """Open first, worst first, newest first — the order a to-do list wants."""
    where: list[str] = []
    params: list[Any] = []
    if state == STATE_OPEN:
        where.append("resolved_at IS NULL")
    elif state == STATE_RESOLVED:
        where.append("resolved_at IS NOT NULL")
    if kind and kind in KINDS:
        where.append("kind = ?")
        params.append(kind)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(int(limit))
    rows = db.fetch_all(
        f"""
        SELECT * FROM issue_log
        {clause}
        ORDER BY (resolved_at IS NULL) DESC,
                 CASE severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
                 last_seen_at DESC
        LIMIT ?
        """,
        params,
    )
    return [_issue_dict(row) for row in rows]


def issue_counts() -> dict[str, Any]:
    row = db.fetch_one(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN resolved_at IS NULL THEN 1 ELSE 0 END) AS open,
               SUM(CASE WHEN resolved_at IS NULL AND severity = 'error' THEN 1 ELSE 0 END) AS errors,
               SUM(CASE WHEN resolved_at IS NULL THEN occurrences ELSE 0 END) AS occurrences
          FROM issue_log
        """
    )
    by_kind = {
        _text(item["kind"]): int(item["amount"] or 0)
        for item in db.fetch_all(
            "SELECT kind, COUNT(*) AS amount FROM issue_log "
            "WHERE resolved_at IS NULL GROUP BY kind"
        )
    }
    total = int((row["total"] if row else 0) or 0)
    open_count = int((row["open"] if row else 0) or 0)
    return {
        "total": total,
        "open": open_count,
        "resolved": total - open_count,
        "errors": int((row["errors"] if row else 0) or 0),
        "occurrences": int((row["occurrences"] if row else 0) or 0),
        "by_kind": {name: by_kind.get(name, 0) for name in KINDS},
    }


def get_issue(issue_id: int) -> dict[str, Any] | None:
    row = db.fetch_one("SELECT * FROM issue_log WHERE id = ?", (int(issue_id),))
    return _issue_dict(row) if row else None


def resolve_issue(issue_id: int, note: str = "") -> bool:
    """"Solved" — stop nagging. It comes back on its own if it happens again."""
    with db.transaction():
        cursor = db.execute(
            "UPDATE issue_log SET resolved_at = ?, resolved_note = ? "
            "WHERE id = ? AND resolved_at IS NULL",
            (utc_now(), _clip(note, 300), int(issue_id)),
        )
    return bool(cursor.rowcount)


def reopen_issue(issue_id: int) -> bool:
    with db.transaction():
        cursor = db.execute(
            "UPDATE issue_log SET resolved_at = NULL, resolved_note = '' "
            "WHERE id = ? AND resolved_at IS NOT NULL",
            (int(issue_id),),
        )
    return bool(cursor.rowcount)


def resolve_all(kind: str = "") -> int:
    params: list[Any] = [utc_now()]
    clause = ""
    if kind and kind in KINDS:
        clause = " AND kind = ?"
        params.append(kind)
    with db.transaction():
        cursor = db.execute(
            "UPDATE issue_log SET resolved_at = ?, resolved_note = 'bulk resolve' "
            f"WHERE resolved_at IS NULL{clause}",
            params,
        )
    return int(cursor.rowcount or 0)


# ---------------------------------------------------------------------------
# the server's own log files
# ---------------------------------------------------------------------------
SERVER_LOGS: tuple[tuple[str, str], ...] = (
    ("server.error.log", "Server errors"),
    ("server.access.log", "Server requests"),
)


def server_log_tail(name: str, lines: int = 200) -> list[str]:
    """Last ``lines`` of a gunicorn log. Reads at most the final 512 KB, so a
    2 MB access log costs nothing to show."""
    path = log_dir() / name
    if not path.is_file():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 512_000))
            chunk = handle.read()
    except OSError as exc:                                 # pragma: no cover
        return [f"(could not read {name}: {exc})"]
    text = chunk.decode("utf-8", errors="replace")
    return [line for line in text.splitlines() if line.strip()][-int(lines):]


def server_log_facts() -> list[dict[str, Any]]:
    facts = []
    for name, label in SERVER_LOGS:
        path = log_dir() / name
        facts.append(
            {
                "name": name,
                "label": label,
                "exists": path.is_file(),
                "size_bytes": path.stat().st_size if path.is_file() else 0,
            }
        )
    return facts


# ---------------------------------------------------------------------------
# "Save current logs" — one click, one file, survives the restart
# ---------------------------------------------------------------------------
def _stamp() -> str:
    return utc_now().replace(":", "").replace("-", "").replace("T", "-")[:15]


def render_snapshot(note: str = "") -> str:
    """The text of a snapshot. Open issues first — that is the section the
    owner reads after a restart to answer "what still needs solving"."""
    counts = issue_counts()
    worker = worker_summary()
    out: list[str] = []
    add = out.append

    add("AdSpy v2 - diagnostics snapshot")
    add(f"taken       : {utc_now()}")
    add(f"app version : {app_config.VERSION}")
    add(f"database    : {_db_file()}")
    add(f"issues      : {counts['open']} open ({counts['errors']} error-level), "
        f"{counts['resolved']} resolved, {counts['total']} total")
    add(f"extension   : {worker['worker_id'] or 'unknown'} - last seen "
        f"{worker['last_seen']}{' - ' + worker['status'] if worker['status'] else ''}")
    if note:
        add(f"note        : {note}")

    add("")
    add("=" * 78)
    add("OPEN ISSUES - what still needs solving")
    add("=" * 78)
    open_issues = list_issues(STATE_OPEN, limit=ISSUE_LIMIT)
    if not open_issues:
        add("(nothing open)")
    for issue in open_issues:
        add("")
        add(f"#{issue['id']} [{issue['severity']}] {issue['kind_label']} "
            f"x{issue['occurrences']}"
            + (f" (reopened {issue['reopened_count']}x)" if issue["reopened_count"] else ""))
        add(f"  what  : {issue['title']}")
        add(f"  where : {issue['location'] or '-'}")
        add(f"  first : {issue['first_seen_at']}")
        add(f"  last  : {issue['last_seen_at']}")
        if issue["detail"]:
            for line in issue["detail"].splitlines():
                add(f"  | {line}")

    add("")
    add("=" * 78)
    add("RECENTLY RESOLVED")
    add("=" * 78)
    resolved = list_issues(STATE_RESOLVED, limit=50)
    if not resolved:
        add("(nothing resolved yet)")
    for issue in resolved:
        add(f"#{issue['id']} [{issue['severity']}] {issue['title']} "
            f"- resolved {issue['resolved_at']} {issue['resolved_note']}".rstrip())

    add("")
    add("=" * 78)
    add("EXTENSION LOG (newest first)")
    add("=" * 78)
    lines = worker_lines(limit=200)
    if not lines:
        add("(the extension has not shipped its log buffer to this server yet)")
    for line in lines:
        add(f"{line['logged_at']} [{line['level']}] {line['message']}")

    for name, label in SERVER_LOGS:
        add("")
        add("=" * 78)
        add(f"{label.upper()} - tail of logs/{name}")
        add("=" * 78)
        tail = server_log_tail(name, 300 if name.endswith("error.log") else 80)
        out.extend(tail or [f"(no {name} on disk)"])

    add("")
    return "\n".join(out) + "\n"


def save_snapshot(note: str = "") -> dict[str, Any]:
    """The one-click capture. Writes ONE file and indexes it in log_snapshots.

    Deliberately a file on disk, not a blob in the database: after a restart
    the owner may want to hand it to somebody, and the database is 213 MB.
    """
    folder = snapshot_dir()
    folder.mkdir(parents=True, exist_ok=True)
    body = render_snapshot(note)

    filename = f"adspy2-logs-{_stamp()}.txt"
    target = folder / filename
    suffix = 2
    while target.exists():
        filename = f"adspy2-logs-{_stamp()}-{suffix}.txt"
        target = folder / filename
        suffix += 1
    target.write_text(body, encoding="utf-8")

    counts = issue_counts()
    size = target.stat().st_size
    with db.transaction():
        cursor = db.execute(
            """
            INSERT INTO log_snapshots(filename, size_bytes, open_issues,
                                      total_issues, note, created_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(filename) DO UPDATE SET size_bytes = excluded.size_bytes
            """,
            (filename, size, counts["open"], counts["total"], _clip(note, 300), utc_now()),
        )
        snapshot_id = int(cursor.lastrowid or 0)
    _trim_snapshots()
    return {
        "id": snapshot_id,
        "filename": filename,
        "path": str(target),
        "size_bytes": size,
        "open_issues": counts["open"],
        "total_issues": counts["total"],
    }


def _trim_snapshots() -> int:
    rows = db.fetch_all(
        "SELECT id, filename FROM log_snapshots ORDER BY created_at DESC, id DESC"
    )
    doomed = rows[SNAPSHOT_LIMIT:]
    if not doomed:
        return 0
    folder = snapshot_dir()
    for row in doomed:
        path = folder / _text(row["filename"])
        try:
            if path.is_file():
                path.unlink()
        except OSError:                                    # pragma: no cover
            log.warning("could not delete old snapshot %s", path)
    with db.transaction():
        db.execute(
            "DELETE FROM log_snapshots WHERE id IN "
            f"({','.join('?' for _ in doomed)})",
            [int(row["id"]) for row in doomed],
        )
    return len(doomed)


def list_snapshots(limit: int = SNAPSHOT_LIMIT) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        "SELECT * FROM log_snapshots ORDER BY created_at DESC, id DESC LIMIT ?",
        (int(limit),),
    )
    folder = snapshot_dir()
    return [
        {
            "id": int(row["id"]),
            "filename": _text(row["filename"]),
            "size_bytes": int(row["size_bytes"] or 0),
            "size_kb": round(int(row["size_bytes"] or 0) / 1024, 1),
            "open_issues": int(row["open_issues"] or 0),
            "total_issues": int(row["total_issues"] or 0),
            "note": _text(row["note"]),
            "created_at": _text(row["created_at"]),
            "when": time_ago(row["created_at"]),
            "exists": (folder / _text(row["filename"])).is_file(),
        }
        for row in rows
    ]


def snapshot_file(snapshot_id: int) -> tuple[Path, str]:
    """Resolve a snapshot id to a path, refusing anything that escapes the
    snapshot folder — the filename comes from the database, but a path that
    leaves its directory is never worth serving."""
    row = db.fetch_one(
        "SELECT filename FROM log_snapshots WHERE id = ?", (int(snapshot_id),)
    )
    if row is None:
        raise LogError("No such saved log file.")
    filename = _text(row["filename"])
    folder = snapshot_dir().resolve()
    path = (folder / filename).resolve()
    if path.parent != folder or not path.is_file():
        raise LogError(f"{filename} is no longer on disk.")
    return path, filename


def delete_snapshot(snapshot_id: int) -> str:
    path_name = ""
    row = db.fetch_one(
        "SELECT filename FROM log_snapshots WHERE id = ?", (int(snapshot_id),)
    )
    if row is None:
        raise LogError("No such saved log file.")
    path_name = _text(row["filename"])
    target = snapshot_dir() / path_name
    try:
        if target.is_file():
            target.unlink()
    except OSError as exc:                                 # pragma: no cover
        raise LogError(f"Could not delete {path_name}: {exc}") from exc
    with db.transaction():
        db.execute("DELETE FROM log_snapshots WHERE id = ?", (int(snapshot_id),))
    return path_name


__all__ = [
    "KINDS",
    "KIND_LABELS",
    "SEVERITIES",
    "STATES",
    "STATE_OPEN",
    "STATE_RESOLVED",
    "STATE_ALL",
    "ISSUE_LIMIT",
    "WORKER_LINE_LIMIT",
    "SNAPSHOT_LIMIT",
    "SWEEP_COOLDOWN_SECONDS",
    "LogError",
    "capture_exception",
    "delete_snapshot",
    "fingerprint_for",
    "get_issue",
    "install_error_capture",
    "issue_counts",
    "list_issues",
    "list_snapshots",
    "log_dir",
    "normalise",
    "record_issue",
    "record_manual_note",
    "record_worker_lines",
    "render_snapshot",
    "reopen_issue",
    "resolve_all",
    "resolve_issue",
    "save_snapshot",
    "server_log_facts",
    "server_log_tail",
    "snapshot_dir",
    "snapshot_file",
    "sweep",
    "time_ago",
    "worker_lines",
    "worker_summary",
]
