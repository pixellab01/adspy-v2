"""Job + lease lifecycle for AdSpy v2.

This module owns everything about a scrape job that is *not* HTTP: creating
jobs from the UI, handing one out under a lease, moving targets through their
states, and deciding what happens when a worker dies or fails. ``app/jobs.py``
is a thin HTTP skin over this file; ``app/routes/queue.py`` (Phase 3) reads the
same functions so the screen and the extension can never disagree.

Three rules from docs/00-decisions.md and docs/04-extension-spec.md are baked in
here and must not be softened:

* **No login, no users, no workspace_id.** Authentication is one shared token in
  the ``settings`` table that the owner copies into the extension
  (``worker_token()``). The real security boundary is the 127.0.0.1 bind.
* **The lease token is never stored.** Only ``sha256(token)`` goes in
  ``jobs.lease_token_hash``. A zombie service worker that wakes up with an old
  token gets 403 instead of double-writing (R10).
* **A page can be queued once.** ``create_job`` refuses to create a second job
  for a page that already has a live target (PRD P0.2). Two overlapping scans of
  one page would race each other's reconciliation.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import uuid
from typing import Any, Iterable, Sequence

from . import config as app_config
from .db import execute, fetch_all, fetch_one, transaction
from .meta_links import meta_ads_library_url
from .time_utils import parse_utc, utc_now, utc_shift

# --- vocabulary ---------------------------------------------------------------
JOB_TYPES = ("page_scan", "keyword")
JOB_ACTIVE_STATUSES = ("pending", "claimed", "running")
JOB_TERMINAL_STATUSES = ("completed", "failed", "cancelled")
TARGET_ACTIVE_STATUSES = ("pending", "running")
TARGET_TERMINAL_STATUSES = ("done", "failed", "skipped")
# R3's six honest outcomes; only the first three may carry isFinal (R4).
FINAL_OUTCOMES = app_config.FINAL_OUTCOMES
VALID_OUTCOMES = app_config.VALID_OUTCOMES
# Outcomes that mean "this target did not finish its work".
BAD_OUTCOMES = ("blocked", "failed")

SETTING_WORKER_TOKEN = "worker_token"
SETTING_QUEUE_PAUSED = "queue_paused"


# ---------------------------------------------------------------------------
# typed errors — every one carries the HTTP status and the code the extension
# switches on, so app/jobs.py never has to guess.
# ---------------------------------------------------------------------------
class JobError(Exception):
    code = "JOB_ERROR"
    status = 400

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status


class AuthError(JobError):
    code = "UNAUTHORIZED"
    status = 401


class NotFoundError(JobError):
    code = "NOT_FOUND"
    status = 404


class LeaseError(JobError):
    """Stale/absent/expired lease — R10's 403."""

    code = "STALE_LEASE"
    status = 403


class DuplicateJobError(JobError):
    code = "PAGE_ALREADY_QUEUED"
    status = 409


class UnscannablePageError(JobError):
    """The page has no numeric Meta page id, so there is no Ad Library URL to
    open and no id to put in a batch (R5).

    This is not hypothetical: 145 of the 394 pages coming from v1 are identified
    only by ``name:<sha1>`` or a vanity slug, because v1 accepted whatever the
    old extension sent. Queuing one of them hands the worker a target it cannot
    open; the Ad Library never renders, and the extension — correctly unable to
    tell "no such page" from "Facebook is stonewalling us" — reports a block and
    walks up R8's backoff ladder until the whole queue halts. Refusing at the
    door turns that into one legible sentence on the screen.
    """

    code = "PAGE_NOT_SCANNABLE"
    status = 409


# ---------------------------------------------------------------------------
# settings (key-value) + the one shared worker token
# ---------------------------------------------------------------------------
def get_setting(key: str, default: str | None = None) -> str | None:
    row = fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
    return None if row is None else str(row["value"])


def set_setting(key: str, value: str) -> None:
    with transaction():
        execute(
            """
            INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at
            """,
            (key, str(value), utc_now()),
        )


def worker_token() -> str:
    """The shared token, created on first read and never rotated behind your
    back. The Queue screen shows this so the owner can paste it into the
    extension's two-field settings panel (docs/04 §2)."""
    existing = (get_setting(SETTING_WORKER_TOKEN) or "").strip()
    if existing:
        return existing
    candidate = secrets.token_urlsafe(32)
    with transaction():
        execute(
            "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES(?, ?, ?)",
            (SETTING_WORKER_TOKEN, candidate, utc_now()),
        )
    return (get_setting(SETTING_WORKER_TOKEN) or candidate).strip()


def rotate_worker_token() -> str:
    """Owner-initiated only. Invalidates the extension until it is re-pasted."""
    token = secrets.token_urlsafe(32)
    set_setting(SETTING_WORKER_TOKEN, token)
    return token


def verify_worker_token(token: Any) -> bool:
    supplied = str(token or "").strip()
    if not supplied:
        return False
    return hmac.compare_digest(worker_token(), supplied)


def is_paused() -> bool:
    return (get_setting(SETTING_QUEUE_PAUSED) or "0").strip() in ("1", "true", "yes")


class DatasetFrozenError(JobError):
    """OLD DATA is active: it is read-only for scans (app/dataset.py). Every
    Track / Re-track / keyword route already surfaces a JobError as a flash."""

    code = "DATASET_FROZEN"
    status = 409


def scan_writes_allowed() -> bool:
    from . import dataset

    return dataset.scan_writes_allowed()


def assert_scan_writes_allowed() -> None:
    if not scan_writes_allowed():
        from . import dataset

        raise DatasetFrozenError(dataset.frozen_message())


def set_paused(paused: bool) -> bool:
    set_setting(SETTING_QUEUE_PAUSED, "1" if paused else "0")
    return bool(paused)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _hash_token(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _lease_expiry() -> str:
    return utc_shift(int(app_config.LEASE_TTL_SECONDS))


def _is_expired(timestamp: Any) -> bool:
    moment = parse_utc(timestamp)
    if moment is None:
        return True
    return moment <= (parse_utc(utc_now()) or moment)


def _placeholders(values: Sequence[Any]) -> str:
    return ",".join("?" for _ in values)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_ids(page_ids: Iterable[Any]) -> list[int]:
    """De-duplicated, order-preserving list of internal page ids."""
    seen: set[int] = set()
    out: list[int] = []
    for raw in page_ids or ():
        value = _int(raw, 0)
        if value > 0 and value not in seen:
            seen.add(value)
            out.append(value)
    return out


# ---------------------------------------------------------------------------
# create — the UI's "Re-track" button lands here
# ---------------------------------------------------------------------------
def create_job(
    page_ids: Iterable[Any],
    *,
    label: str | None = None,
    job_type: str = "page_scan",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Queue one page-scan job covering ``page_ids`` in the order given.

    Raises ``DuplicateJobError`` when any of those pages already has a live
    target on a pending/claimed/running job — PRD P0.2. One page, one scan.
    """
    if job_type not in JOB_TYPES:
        raise JobError(f"unknown job type {job_type!r}", code="BAD_JOB_TYPE")
    assert_scan_writes_allowed()

    ids = _clean_ids(page_ids)
    if not ids:
        raise JobError("a job needs at least one page", code="NO_TARGETS")

    now = utc_now()
    with transaction():
        rows = fetch_all(
            f"SELECT id, platform_page_id, name, alias, url FROM pages "
            f"WHERE id IN ({_placeholders(ids)})",
            ids,
        )
        by_id = {int(r["id"]): r for r in rows}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise NotFoundError(f"unknown page id(s): {missing}")

        if job_type == "page_scan":
            unscannable = [
                by_id[i] for i in ids
                if not meta_ads_library_url(by_id[i]["platform_page_id"], by_id[i]["url"])
            ]
            if unscannable:
                names = ", ".join(
                    target_label(r["alias"], r["name"], r["platform_page_id"])
                    for r in unscannable[:4]
                )
                raise UnscannablePageError(
                    f"no Meta page id for: {names} — the Ad Library needs a numeric "
                    "view_all_page_id and this page has none, so there is nothing to open. "
                    "Open the page in the Ad Library and re-add it from that URL."
                )

        clashes = fetch_all(
            f"""
            SELECT DISTINCT t.page_id AS page_id, t.job_id AS job_id
            FROM job_targets t
            JOIN jobs j ON j.id = t.job_id
            WHERE t.page_id IN ({_placeholders(ids)})
              AND j.status IN {JOB_ACTIVE_STATUSES}
              AND t.status IN {TARGET_ACTIVE_STATUSES}
            """,
            ids,
        )
        if clashes:
            names = ", ".join(
                str(by_id[int(c["page_id"])]["name"] or by_id[int(c["page_id"])]["platform_page_id"])
                for c in clashes
                if int(c["page_id"]) in by_id
            )
            raise DuplicateJobError(
                f"already queued: {names or 'page'} "
                f"(job {clashes[0]['job_id']}) — wait for it or cancel it first"
            )

        cursor = execute(
            """
            INSERT INTO jobs(job_type, status, idempotency_key, label, retryable,
                             retry_count, max_retries, targets_total, targets_done,
                             created_at, updated_at)
            VALUES(?, 'pending', ?, ?, 0, 0, ?, ?, 0, ?, ?)
            """,
            (
                job_type,
                (idempotency_key or uuid.uuid4().hex),
                label or _default_label(rows, ids),
                int(app_config.JOB_MAX_RETRIES),
                len(ids),
                now,
                now,
            ),
        )
        job_id = int(cursor.lastrowid)

        for position, page_id in enumerate(ids, start=1):
            page = by_id[page_id]
            platform_page_id = str(page["platform_page_id"] or "")
            page_url = (
                meta_ads_library_url(platform_page_id, page["url"])
                or str(page["url"] or "")
            )
            execute(
                """
                INSERT INTO job_targets(job_id, position, page_id, platform_page_id,
                                        page_url, label, status, estimated_results)
                VALUES(?, ?, ?, ?, ?, ?, 'pending',
                       (SELECT COALESCE(fb_estimated_results, 0) FROM pages WHERE id = ?))
                """,
                (
                    job_id,
                    position,
                    page_id,
                    platform_page_id,
                    page_url,
                    target_label(page["alias"], page["name"], platform_page_id),
                    page_id,
                ),
            )

        execute(
            f"UPDATE pages SET current_scan_status = 'queued', updated_at = ? "
            f"WHERE id IN ({_placeholders(ids)})",
            [now, *ids],
        )

    return get_job(job_id)


def target_label(alias: Any, name: Any, platform_page_id: Any) -> str:
    """What the Queue screen calls this page.

    A5. This was ``alias or name or platform_page_id``, and 145+ pages imported
    from v1 carry a NUMERIC STRING in ``pages.name`` — the page's own platform
    id, stored as its name because v1 never learned a better one. The fallback
    chain therefore stopped at ``name`` and produced a bare number, and because
    several distinct pages share the *same* stale numeric name, live job 4
    listed eight different pages all labelled ``781596618368985`` and eight
    more all labelled ``947334221805509``. On the Queue screen that reads as
    the same page queued sixteen times — a working queue that looks broken.

    So a numeric ``name`` is not a name. Fall through it to a label that is at
    least honest about being an id: ``Page 350759231463449``.
    """
    text = str(alias or "").strip()
    if text:
        return text
    text = str(name or "").strip()
    if text and not text.isdigit():
        return text
    platform = str(platform_page_id or "").strip()
    return f"Page {platform}" if platform else "Page"


def _default_label(rows: Sequence[sqlite3.Row], ids: Sequence[int]) -> str:
    by_id = {int(r["id"]): r for r in rows}
    first = by_id.get(ids[0])
    name = target_label(
        first["alias"] if first else "",
        first["name"] if first else "",
        first["platform_page_id"] if first else "",
    )
    return name if len(ids) == 1 else f"{name} +{len(ids) - 1} more"


# ---------------------------------------------------------------------------
# claim — hand out exactly one job, under a lease
# ---------------------------------------------------------------------------
def claim_next_job(installation_id: str = "") -> dict[str, Any]:
    """Return ``{"job": {...}, "leaseToken": "..."}`` or, when there is nothing
    to hand out, ``{"job": None, "reason": <typed>}``.

    Typed refusals: ``queue_paused``, ``queue_empty``, ``already_leased``
    (work exists but another worker holds a live lease), ``claim_race``.
    """
    reap_expired_leases()

    if is_paused():
        return {"job": None, "reason": "queue_paused"}

    lease_token = secrets.token_urlsafe(32)
    now = utc_now()

    with transaction():
        row = fetch_one(
            "SELECT id FROM jobs WHERE status = 'pending' ORDER BY id LIMIT 1"
        )
        if row is None:
            leased = fetch_one(
                """
                SELECT id FROM jobs
                WHERE status IN ('claimed','running')
                  AND (lease_expires_at IS NULL OR lease_expires_at > ?)
                LIMIT 1
                """,
                (now,),
            )
            return {"job": None, "reason": "already_leased" if leased else "queue_empty"}

        job_id = int(row["id"])
        cursor = execute(
            """
            UPDATE jobs
               SET status = 'claimed',
                   lease_token_hash = ?,
                   lease_expires_at = ?,
                   claimed_at = ?,
                   started_at = COALESCE(started_at, ?),
                   error = NULL,
                   error_code = NULL,
                   cancel_requested_at = NULL,
                   updated_at = ?
             WHERE id = ? AND status = 'pending'
            """,
            (_hash_token(lease_token), _lease_expiry(), now, now, now, job_id),
        )
        if cursor.rowcount != 1:
            # Belt and braces: BEGIN IMMEDIATE already serialises writers, so
            # this should be unreachable. If it ever fires, the worker retries.
            return {"job": None, "reason": "claim_race"}

        # Resume: whatever a dead worker left mid-flight goes back to pending
        # so the new lease holder re-runs it (docs/04 §4 claim).
        execute(
            "UPDATE job_targets SET status = 'pending', started_at = NULL "
            "WHERE job_id = ? AND status = 'running'",
            (job_id,),
        )
        job = _serialize_job_for_worker(job_id)

    return {"job": job, "leaseToken": lease_token, "reason": "claimed"}


def reap_expired_leases() -> int:
    """Return jobs whose lease died with their worker to the queue. A reaped
    lease burns a retry so a permanently crashing worker cannot loop forever."""
    now = utc_now()
    rows = fetch_all(
        """
        SELECT id, retry_count, max_retries FROM jobs
        WHERE status IN ('claimed','running')
          AND lease_expires_at IS NOT NULL
          AND lease_expires_at <= ?
        """,
        (now,),
    )
    if not rows:
        return 0

    with transaction():
        for row in rows:
            job_id = int(row["id"])
            exhausted = _int(row["retry_count"]) + 1 > _int(row["max_retries"])
            if exhausted:
                execute(
                    """
                    UPDATE jobs SET status = 'failed', outcome = 'failed',
                                    error = 'worker lease expired',
                                    error_code = 'LEASE_EXPIRED',
                                    retryable = 0, finished_at = ?,
                                    lease_token_hash = NULL, lease_expires_at = NULL,
                                    updated_at = ?
                     WHERE id = ?
                    """,
                    (now, now, job_id),
                )
                execute(
                    "UPDATE job_targets SET status = 'failed', outcome = 'failed', "
                    "message = 'worker lease expired', finished_at = ? "
                    "WHERE job_id = ? AND status IN ('pending','running')",
                    (now, job_id),
                )
            else:
                execute(
                    """
                    UPDATE jobs SET status = 'pending', retry_count = retry_count + 1,
                                    claimed_at = NULL, lease_token_hash = NULL,
                                    lease_expires_at = NULL, updated_at = ?
                     WHERE id = ?
                    """,
                    (now, job_id),
                )
                execute(
                    "UPDATE job_targets SET status = 'pending', started_at = NULL "
                    "WHERE job_id = ? AND status = 'running'",
                    (job_id,),
                )
            _sync_page_status(job_id)
    return len(rows)


# ---------------------------------------------------------------------------
# lease validation + renewal
# ---------------------------------------------------------------------------
def validate_lease(job_id: int, lease_token: Any) -> sqlite3.Row:
    """Every per-job call goes through here. Wrong/expired token ⇒ 403 (R10)."""
    row = fetch_one("SELECT * FROM jobs WHERE id = ?", (int(job_id),))
    if row is None:
        raise NotFoundError(f"job {job_id} does not exist")
    if row["status"] in JOB_TERMINAL_STATUSES:
        raise LeaseError(
            f"job {job_id} is already {row['status']}", code="JOB_FINISHED"
        )

    supplied = str(lease_token or "").strip()
    stored = str(row["lease_token_hash"] or "")
    if not supplied or not stored:
        raise LeaseError("missing lease token")
    if not hmac.compare_digest(stored, _hash_token(supplied)):
        raise LeaseError("lease token does not match the current lease holder")
    if _is_expired(row["lease_expires_at"]):
        raise LeaseError("lease has expired", code="LEASE_EXPIRED")
    return row


def renew_lease(job_id: int) -> str:
    expires_at = _lease_expiry()
    with transaction():
        execute(
            "UPDATE jobs SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
            (expires_at, utc_now(), int(job_id)),
        )
    return expires_at


def command_for(current_job_id: Any = None) -> str | None:
    """``continue | pause | cancel_job | null`` for /hello."""
    if is_paused():
        return "pause"
    job_id = _int(current_job_id, 0)
    if job_id <= 0:
        return None
    row = fetch_one(
        "SELECT status, cancel_requested_at FROM jobs WHERE id = ?", (job_id,)
    )
    if row is None or row["cancel_requested_at"] or row["status"] in JOB_TERMINAL_STATUSES:
        return "cancel_job"
    return "continue"


def command_for_running_job(job_id: int) -> str:
    """``continue | cancel_job`` for /status and /batch (never ``pause``: a
    running page is finished before the worker rests)."""
    row = fetch_one(
        "SELECT status, cancel_requested_at FROM jobs WHERE id = ?", (int(job_id),)
    )
    if row is None or row["cancel_requested_at"] or row["status"] in JOB_TERMINAL_STATUSES:
        return "cancel_job"
    return "continue"


# ---------------------------------------------------------------------------
# collector groups
# ---------------------------------------------------------------------------
def _fill_collector_groups(page_id: Any, now: str) -> list[int]:
    """Link a freshly-scanned page into every group that collects from a group
    it already belongs to.

    Called only for outcomes in FINAL_OUTCOMES — the same three that let ingest
    retire stopped ads. That is the point: membership of a collector group is a
    promise that THIS page's ad list was verified end to end, so "Naaptol new"
    means "re-scanned", not merely "queued". A partial or blocked scan leaves
    the page where it was, which is what makes the two group counts an honest
    progress bar.

    Returns the collector group ids the page was newly added to.
    """
    page = _int(page_id)
    if page <= 0:
        return []

    rows = fetch_all(
        """
        SELECT DISTINCT collector.id AS id
          FROM groups AS collector
          JOIN group_pages AS source
            ON source.group_id = collector.collect_from_group_id
         WHERE collector.collect_from_group_id IS NOT NULL
           AND source.page_id = ?
           AND collector.id <> source.group_id
        """,
        (page,),
    )

    added: list[int] = []
    for row in rows:
        cursor = execute(
            "INSERT OR IGNORE INTO group_pages(group_id, page_id, added_at) "
            "VALUES(?,?,?)",
            (int(row["id"]), page, now),
        )
        if cursor.rowcount:
            added.append(int(row["id"]))
    return added


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------
def record_target_progress(
    job_id: int,
    *,
    position: Any,
    state: str,
    outcome: str | None = None,
    scrolls: Any = 0,
    unique_ads: Any = 0,
    represented_ads: Any = 0,
    message: str = "",
) -> dict[str, Any]:
    """/status: per-target live truth for the dashboard row.

    A3: ``represented_ads`` is cumulative for the target, so the Queue row
    shows ads-vs-cards honestly between batches too, not only after one lands.
    """
    if state not in ("running", "done"):
        raise JobError(f"state must be running|done, got {state!r}", code="BAD_STATE")
    outcome_value = (str(outcome).strip() or None) if outcome else None
    if outcome_value and outcome_value not in VALID_OUTCOMES:
        raise JobError(f"unknown outcome {outcome_value!r}", code="BAD_OUTCOME")

    job_id = int(job_id)
    target = _target_row(job_id, position)
    now = utc_now()

    with transaction():
        if state == "running":
            execute(
                """
                UPDATE job_targets
                   SET status = 'running',
                       started_at = COALESCE(started_at, ?),
                       scrolls = MAX(scrolls, ?),
                       unique_ads = MAX(unique_ads, ?),
                       represented_ads = MAX(represented_ads, ?),
                       message = CASE WHEN ? <> '' THEN ? ELSE message END
                 WHERE id = ?
                """,
                (
                    now,
                    _int(scrolls),
                    _int(unique_ads),
                    _int(represented_ads),
                    message,
                    message,
                    int(target["id"]),
                ),
            )
            execute(
                "UPDATE jobs SET status = 'running', started_at = COALESCE(started_at, ?), "
                "updated_at = ? WHERE id = ? AND status IN ('claimed','running')",
                (now, now, job_id),
            )
        else:
            final_status = "failed" if outcome_value in BAD_OUTCOMES else "done"
            execute(
                """
                UPDATE job_targets
                   SET status = ?,
                       outcome = COALESCE(?, outcome),
                       scrolls = MAX(scrolls, ?),
                       unique_ads = MAX(unique_ads, ?),
                       represented_ads = MAX(represented_ads, ?),
                       message = CASE WHEN ? <> '' THEN ? ELSE message END,
                       finished_at = ?
                 WHERE id = ?
                """,
                (
                    final_status,
                    outcome_value,
                    _int(scrolls),
                    _int(unique_ads),
                    _int(represented_ads),
                    message,
                    message,
                    now,
                    int(target["id"]),
                ),
            )

        execute(
            "UPDATE jobs SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
            (_lease_expiry(), now, job_id),
        )
        _recount_targets(job_id)
        _sync_page_status(job_id)
        if state == "done" and outcome_value in FINAL_OUTCOMES:
            _fill_collector_groups(target["page_id"], now)

    counts = _target_counts(job_id)
    return {
        "command": command_for_running_job(job_id),
        "leaseExpiresAt": _job_field(job_id, "lease_expires_at"),
        "targetsDone": counts["done"],
        "targetsTotal": counts["total"],
    }


def record_batch_progress(
    job_id: int,
    *,
    position: Any,
    page_id: Any = None,
    represented_ads: Any = 0,
    estimated_results: Any = 0,
    unique_ads: Any = 0,
) -> None:
    """Counters that arrive on /batch rather than /status.

    Counts never go down — batches can land out of order after a parked-batch
    replay (R9). ``estimated_results`` is the exception: the header estimate
    from the scan in progress is fresher than the one stored on the page, so a
    non-zero value replaces it outright.

    A3. ``represented_ads`` MUST be the target's running total, not this
    batch's. ``MAX`` over per-batch sums is what made the Queue screen read
    ``unique_ads=251 / represented_ads=50`` on job 4 position 1 — 251 ads whose
    cards stood for "50", which is not a number that exists anywhere. The
    extension now sends ``representedAdTotal`` (its cumulative
    ``state.represented`` for the target) alongside the per-batch
    ``representedAdCount``, so MAX is once again a monotone read of a
    cumulative counter, exactly as it already is for ``unique_ads``.

    MAX, not ``+=``: a job that is retried re-scans the same target onto the
    same row, and a sum would double it.
    """
    target = _target_row(job_id, position, required=False)
    if target is None:
        return
    with transaction():
        execute(
            """
            UPDATE job_targets
               SET page_id = COALESCE(?, page_id),
                   represented_ads = MAX(represented_ads, ?),
                   estimated_results = CASE WHEN ? > 0 THEN ? ELSE estimated_results END,
                   unique_ads = MAX(unique_ads, ?)
             WHERE id = ?
            """,
            (
                _int(page_id) or None,
                _int(represented_ads),
                _int(estimated_results),
                _int(estimated_results),
                _int(unique_ads),
                int(target["id"]),
            ),
        )


# ---------------------------------------------------------------------------
# finish
# ---------------------------------------------------------------------------
def finish_job(
    job_id: int,
    *,
    outcome: str,
    error: str = "",
    error_code: str = "",
    retryable: bool = False,
) -> dict[str, Any]:
    """/done. A retryable failure with retries left puts the job back to
    ``pending`` and returns its unfinished targets to ``pending`` too; anything
    else is terminal, and a job whose targets are all terminal is completed."""
    if outcome not in ("completed", "failed"):
        raise JobError(
            f"done outcome must be completed|failed, got {outcome!r}",
            code="BAD_OUTCOME",
        )

    job_id = int(job_id)
    row = fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if row is None:
        raise NotFoundError(f"job {job_id} does not exist")

    now = utc_now()
    cancelled = bool(row["cancel_requested_at"])
    retry_left = _int(row["retry_count"]) < _int(row["max_retries"])
    will_retry = outcome == "failed" and bool(retryable) and retry_left and not cancelled

    with transaction():
        if will_retry:
            execute(
                """
                UPDATE jobs
                   SET status = 'pending', outcome = NULL,
                       retry_count = retry_count + 1, retryable = 1,
                       error = ?, error_code = ?,
                       claimed_at = NULL, lease_token_hash = NULL,
                       lease_expires_at = NULL, finished_at = NULL, updated_at = ?
                 WHERE id = ?
                """,
                (str(error or ""), str(error_code or ""), now, job_id),
            )
            # Unfinished work goes back in the queue; already-scanned targets stay done.
            execute(
                """
                UPDATE job_targets
                   SET status = 'pending', outcome = NULL,
                       started_at = NULL, finished_at = NULL
                 WHERE job_id = ? AND status IN ('running','failed')
                """,
                (job_id,),
            )
            final_status = "pending"
        else:
            final_status = (
                "cancelled" if cancelled else ("failed" if outcome == "failed" else "completed")
            )
            # Anything never reached is skipped, so "all targets terminal" holds.
            execute(
                """
                UPDATE job_targets
                   SET status = 'skipped', finished_at = ?,
                       message = CASE WHEN COALESCE(message,'') = ''
                                      THEN 'not run — job ended' ELSE message END
                 WHERE job_id = ? AND status IN ('pending','running')
                """,
                (now, job_id),
            )
            execute(
                """
                UPDATE jobs
                   SET status = ?, outcome = ?, error = ?, error_code = ?,
                       retryable = ?, finished_at = ?, lease_token_hash = NULL,
                       lease_expires_at = NULL, updated_at = ?
                 WHERE id = ?
                """,
                (
                    final_status,
                    outcome,
                    str(error or ""),
                    str(error_code or ""),
                    1 if retryable else 0,
                    now,
                    now,
                    job_id,
                ),
            )
        _recount_targets(job_id)
        _sync_page_status(job_id)

    counts = _target_counts(job_id)
    return {
        "jobId": job_id,
        "status": final_status,
        "retryScheduled": will_retry,
        "retryCount": _int(_job_field(job_id, "retry_count")),
        "targetsDone": counts["done"],
        "targetsTotal": counts["total"],
    }


def cancel_job(job_id: int) -> dict[str, Any]:
    """Ask the worker to stop. A job nobody has claimed dies immediately; a
    leased one is flagged and the next /status or /hello returns cancel_job."""
    job_id = int(job_id)
    row = fetch_one("SELECT status FROM jobs WHERE id = ?", (job_id,))
    if row is None:
        raise NotFoundError(f"job {job_id} does not exist")
    if row["status"] in JOB_TERMINAL_STATUSES:
        return get_job(job_id)

    now = utc_now()
    with transaction():
        if row["status"] == "pending":
            execute(
                "UPDATE jobs SET status = 'cancelled', cancel_requested_at = ?, "
                "finished_at = ?, lease_token_hash = NULL, lease_expires_at = NULL, "
                "updated_at = ? WHERE id = ?",
                (now, now, now, job_id),
            )
            execute(
                "UPDATE job_targets SET status = 'skipped', finished_at = ? "
                "WHERE job_id = ? AND status IN ('pending','running')",
                (now, job_id),
            )
        else:
            execute(
                "UPDATE jobs SET cancel_requested_at = ?, updated_at = ? WHERE id = ?",
                (now, now, job_id),
            )
        _recount_targets(job_id)
        _sync_page_status(job_id)
    return get_job(job_id)


def retry_job(job_id: int) -> dict[str, Any]:
    """Queue screen's Retry button: a finished job goes back to pending with
    its unfinished targets reset. Targets that already scanned stay done."""
    job_id = int(job_id)
    row = fetch_one("SELECT status FROM jobs WHERE id = ?", (job_id,))
    if row is None:
        raise NotFoundError(f"job {job_id} does not exist")
    if row["status"] in JOB_ACTIVE_STATUSES:
        raise JobError(
            f"job {job_id} is still {row['status']} — nothing to retry",
            code="JOB_NOT_FINISHED",
            status=409,
        )

    now = utc_now()
    with transaction():
        execute(
            """
            UPDATE jobs
               SET status = 'pending', outcome = NULL, error = NULL, error_code = NULL,
                   retry_count = retry_count + 1, retryable = 0,
                   claimed_at = NULL, finished_at = NULL, cancel_requested_at = NULL,
                   lease_token_hash = NULL, lease_expires_at = NULL, updated_at = ?
             WHERE id = ?
            """,
            (now, job_id),
        )
        execute(
            """
            UPDATE job_targets
               SET status = 'pending', outcome = NULL, message = NULL,
                   started_at = NULL, finished_at = NULL
             WHERE job_id = ? AND status <> 'done'
            """,
            (job_id,),
        )
        _recount_targets(job_id)
        _sync_page_status(job_id)
    return get_job(job_id)


# ---------------------------------------------------------------------------
# worker registry (D1)
#
# The `workers` table existed from migration 002 and nothing ever wrote to it:
# 0 rows on 2026-08-16 against a database with 20,183 ads. That is the whole
# shape of the owner's "the plugin is not working any more" — a worker that
# halts on a captcha goes silent, and the dashboard cannot tell "halted three
# hours ago on a login wall" from "the owner closed Chrome". Every /hello and
# every /claim now leaves a row behind, so the Logs screen can answer it.
#
# Deliberately forgiving: a registry write must NEVER be the reason a scrape
# fails, so every error here is swallowed. The scrape is the product; this is
# bookkeeping about the scrape.
# ---------------------------------------------------------------------------
WORKER_ONLINE_STATES = ("idle", "claiming", "running", "paused")


def _worker_status(state: str) -> str:
    """Extension state -> the `workers.status` CHECK vocabulary."""
    value = str(state or "").strip().lower()
    if value in ("halted", "error", "blocked"):
        return "error"
    if value in WORKER_ONLINE_STATES:
        return "online"
    return "online" if value else "offline"


def upsert_worker(
    worker_id: Any,
    *,
    state: str = "",
    extension_version: Any = "",
    current_job_id: Any = None,
    last_error: Any = "",
) -> bool:
    """Record that this worker just spoke to us. Returns True when a row landed.

    ``worker_id`` is the extension's ``installationId``. An empty one is not an
    error — an old extension build does not send it — it just cannot be
    attributed, so nothing is written.
    """
    key = str(worker_id or "").strip()[:80]
    if not key:
        return False
    now = utc_now()
    status = _worker_status(state)
    version = str(extension_version or "").strip()[:40]
    error = str(last_error or "").strip()[:300]
    job_id = _int(current_job_id, 0) or None
    try:
        # A job id that no longer exists would trip the FK, and a stale
        # currentJobId from the extension is normal after a job is deleted.
        if job_id is not None and fetch_one("SELECT 1 FROM jobs WHERE id = ?", (job_id,)) is None:
            job_id = None
        execute(
            """
            INSERT INTO workers(
                worker_id, display_name, status, extension_version,
                current_job_id, last_error, last_heartbeat_at,
                first_seen_at, last_seen_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(worker_id) DO UPDATE SET
                status            = excluded.status,
                extension_version = CASE WHEN excluded.extension_version <> ''
                                         THEN excluded.extension_version
                                         ELSE workers.extension_version END,
                current_job_id    = excluded.current_job_id,
                -- A halt reason is sticky: it stays until the worker comes back
                -- healthy, so the owner still sees WHY it stopped hours later.
                last_error        = CASE WHEN excluded.last_error <> ''
                                         THEN excluded.last_error
                                         WHEN excluded.status = 'online' THEN ''
                                         ELSE workers.last_error END,
                last_heartbeat_at = excluded.last_heartbeat_at,
                last_seen_at      = excluded.last_seen_at
            """,
            (key, key, status, version, job_id, error, now, now, now),
        )
        return True
    except Exception:  # noqa: BLE001 - bookkeeping never breaks a scrape
        return False


def worker_registry(limit: int = 10) -> list[dict[str, Any]]:
    """Newest-first view of `workers`, for the Logs screen."""
    rows = fetch_all(
        "SELECT * FROM workers ORDER BY COALESCE(last_heartbeat_at, last_seen_at) DESC LIMIT ?",
        (max(1, _int(limit, 10)),),
    )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# reads for the UI
# ---------------------------------------------------------------------------
def queued_job_count() -> int:
    row = fetch_one("SELECT COUNT(*) AS n FROM jobs WHERE status = 'pending'")
    return _int(row["n"]) if row else 0


def list_jobs(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = (
        "SELECT j.*, "
        "  (SELECT COUNT(*) FROM job_targets t WHERE t.job_id = j.id) AS target_count "
        "FROM jobs j"
    )
    params: list[Any] = []
    if status == "active":
        sql += f" WHERE j.status IN {JOB_ACTIVE_STATUSES}"
    elif status:
        sql += " WHERE j.status = ?"
        params.append(status)
    sql += " ORDER BY j.id DESC LIMIT ?"
    params.append(max(1, _int(limit, 50)))
    return [_job_summary(row) for row in fetch_all(sql, params)]


def get_job(job_id: int) -> dict[str, Any]:
    row = fetch_one("SELECT * FROM jobs WHERE id = ?", (int(job_id),))
    if row is None:
        raise NotFoundError(f"job {job_id} does not exist")
    payload = _job_summary(row)
    payload["targets"] = [
        _target_summary(t)
        for t in fetch_all(
            "SELECT * FROM job_targets WHERE job_id = ? ORDER BY position",
            (int(job_id),),
        )
    ]
    return payload


def job_status_report(job_id: int) -> dict[str, Any]:
    """Everything the live queue row needs, incl. per-target coverage."""
    payload = get_job(job_id)
    for target in payload["targets"]:
        estimate = _int(target.get("estimatedResults"))
        unique = _int(target.get("uniqueAds"))
        target["coverage"] = round(unique / estimate, 4) if estimate > 0 else None
    return payload


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------
def _target_row(job_id: int, position: Any, required: bool = True) -> sqlite3.Row | None:
    row = fetch_one(
        "SELECT * FROM job_targets WHERE job_id = ? AND position = ?",
        (int(job_id), _int(position, -1)),
    )
    if row is None and required:
        raise NotFoundError(f"job {job_id} has no target at position {position}")
    return row


def _job_field(job_id: int, column: str) -> Any:
    row = fetch_one(f"SELECT {column} AS value FROM jobs WHERE id = ?", (int(job_id),))
    return None if row is None else row["value"]


def _target_counts(job_id: int) -> dict[str, int]:
    row = fetch_one(
        f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status IN {TARGET_TERMINAL_STATUSES} THEN 1 ELSE 0 END) AS done
        FROM job_targets WHERE job_id = ?
        """,
        (int(job_id),),
    )
    return {"total": _int(row["total"]) if row else 0, "done": _int(row["done"]) if row else 0}


def _recount_targets(job_id: int) -> None:
    counts = _target_counts(job_id)
    execute(
        "UPDATE jobs SET targets_total = ?, targets_done = ?, updated_at = ? WHERE id = ?",
        (counts["total"], counts["done"], utc_now(), int(job_id)),
    )


def _sync_page_status(job_id: int) -> None:
    """Keep ``pages.current_scan_status`` honest — it drives the pages list."""
    now = utc_now()
    rows = fetch_all(
        "SELECT page_id, status FROM job_targets WHERE job_id = ? AND page_id IS NOT NULL",
        (int(job_id),),
    )
    for row in rows:
        target_status = str(row["status"])
        if target_status == "running":
            page_status = "running"
        elif target_status == "pending":
            page_status = "queued"
        elif target_status == "failed":
            page_status = "error"
        else:
            page_status = "idle"
        # Never demote a page that another live job still has queued/running.
        if page_status == "idle":
            other = fetch_one(
                f"""
                SELECT 1 FROM job_targets t JOIN jobs j ON j.id = t.job_id
                WHERE t.page_id = ? AND t.job_id <> ?
                  AND j.status IN {JOB_ACTIVE_STATUSES}
                  AND t.status IN {TARGET_ACTIVE_STATUSES}
                LIMIT 1
                """,
                (int(row["page_id"]), int(job_id)),
            )
            if other:
                continue
        execute(
            "UPDATE pages SET current_scan_status = ?, updated_at = ? WHERE id = ?",
            (page_status, now, int(row["page_id"])),
        )


def _serialize_job_for_worker(job_id: int) -> dict[str, Any]:
    """The claim payload — exactly the shape docs/04 §4 promises."""
    job = fetch_one("SELECT * FROM jobs WHERE id = ?", (int(job_id),))
    targets = fetch_all(
        """
        SELECT t.position, t.platform_page_id, t.page_url, t.label,
               COALESCE(p.fb_estimated_results, 0) AS estimated_results
        FROM job_targets t
        LEFT JOIN pages p ON p.id = t.page_id
        WHERE t.job_id = ? AND t.status = 'pending'
        ORDER BY t.position
        """,
        (int(job_id),),
    )
    payload_targets = [
        {
            "position": _int(t["position"]),
            "pageId": str(t["platform_page_id"] or ""),
            "pageUrl": str(t["page_url"] or ""),
            "label": str(t["label"] or ""),
            "estimatedResults": _int(t["estimated_results"]),
        }
        for t in targets
    ]
    return {
        "jobId": int(job_id),
        "jobType": str(job["job_type"]),
        "label": str(job["label"] or ""),
        "targets": payload_targets,
        "estimatedResults": sum(t["estimatedResults"] for t in payload_targets),
    }


def _job_summary(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    return {
        "jobId": _int(row["id"]),
        "jobType": str(row["job_type"]),
        "status": str(row["status"]),
        "label": str(row["label"] or ""),
        "outcome": row["outcome"],
        "error": row["error"],
        "errorCode": row["error_code"],
        "retryable": bool(_int(row["retryable"])),
        "retryCount": _int(row["retry_count"]),
        "maxRetries": _int(row["max_retries"]),
        "targetsTotal": _int(row["targets_total"]),
        "targetsDone": _int(row["targets_done"]),
        "targetCount": _int(row["target_count"]) if "target_count" in keys else None,
        "leaseExpiresAt": row["lease_expires_at"],
        "cancelRequestedAt": row["cancel_requested_at"],
        "createdAt": row["created_at"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
    }


def _target_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "position": _int(row["position"]),
        "pageId": str(row["platform_page_id"] or ""),
        "pageDbId": row["page_id"],
        "pageUrl": str(row["page_url"] or ""),
        "label": str(row["label"] or ""),
        "status": str(row["status"]),
        "outcome": row["outcome"],
        "scrolls": _int(row["scrolls"]),
        "uniqueAds": _int(row["unique_ads"]),
        "representedAds": _int(row["represented_ads"]),
        "estimatedResults": _int(row["estimated_results"]),
        "message": row["message"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
    }


# ---------------------------------------------------------------------------
# hello additions — extension REFRESH + clean resume (2026-09-09)
#
# The side panel used to rebuild its queue list only from chrome.storage, and
# nothing ever cleared that key, so rows from a job finished 12 days earlier
# were still on screen ("815/~250 complete"). /hello now carries the dashboard's
# truth — the job THIS worker holds, and every live job — so the panel can
# rebuild from it on every heartbeat and on the Refresh button. All of it is
# ADDITIVE: an older extension ignores fields it does not know.
# ---------------------------------------------------------------------------
def live_job_ids() -> list[int]:
    """Ids of every pending / claimed / running job, ascending."""
    rows = fetch_all(
        f"SELECT id FROM jobs WHERE status IN {JOB_ACTIVE_STATUSES} ORDER BY id"
    )
    return [int(r["id"]) for r in rows]


def live_jobs_summary(limit: int = 20) -> list[dict[str, Any]]:
    """The compact list the panel shows while idle: what the dashboard has
    queued, so the owner can see the queue without opening the dashboard."""
    rows = fetch_all(
        f"""
        SELECT j.*, (SELECT COUNT(*) FROM job_targets t WHERE t.job_id = j.id) AS target_count
          FROM jobs j
         WHERE j.status IN {JOB_ACTIVE_STATUSES}
         ORDER BY j.id
         LIMIT ?
        """,
        (max(1, _int(limit, 20)),),
    )
    return [
        {
            "jobId": _int(row["id"]),
            "jobType": str(row["job_type"]),
            "status": str(row["status"]),
            "label": str(row["label"] or ""),
            "targetsDone": _int(row["targets_done"]),
            "targetsTotal": _int(row["targets_total"]) or _int(row["target_count"]),
            "retryCount": _int(row["retry_count"]),
            "leaseExpiresAt": row["lease_expires_at"],
        }
        for row in rows
    ]


def _lease_is_live(row: sqlite3.Row) -> bool:
    return (
        str(row["status"]) in ("claimed", "running")
        and bool(row["lease_token_hash"])
        and not _is_expired(row["lease_expires_at"])
    )


def _registry_job_id(installation_id: Any) -> int:
    key = str(installation_id or "").strip()[:80]
    if not key:
        return 0
    row = fetch_one("SELECT current_job_id FROM workers WHERE worker_id = ?", (key,))
    return _int(row["current_job_id"]) if row else 0


def active_job_for_worker(installation_id: Any, current_job_id: Any = None) -> dict[str, Any] | None:
    """``job_status_report`` for the job this worker holds under a LIVE lease,
    else ``None``.

    The id the extension reports wins; when it reports none (an idle hello
    after a service-worker restart) the ``workers`` registry is consulted, so a
    worker that forgot its job is told about it rather than left to guess. A
    job whose lease has lapsed is deliberately NOT reported: ``reap_expired_leases``
    is about to hand it to whoever claims next, and the panel must not paint
    it as "mine".
    """
    job_id = _int(current_job_id, 0)
    if job_id <= 0:
        job_id = _registry_job_id(installation_id)
    if job_id <= 0:
        return None
    row = fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if row is None or not _lease_is_live(row):
        return None
    return job_status_report(job_id)


def release_lease(job_id: Any, installation_id: Any = "") -> dict[str, Any]:
    """The extension still knows WHICH job it held but lost the lease token
    (R10 keeps it in ``storage.session``, which Chrome wipes on restart). Put
    the job straight back to ``pending`` — targets left ``running`` go back too
    — WITHOUT touching ``retry_count``.

    Before this, the worker had to sit out the remaining lease (≤5 min of
    ``already_leased`` refusals) until ``reap_expired_leases`` returned the job
    with ``retry_count + 1``: three Chrome restarts on one long job made it a
    permanent ``LEASE_EXPIRED`` failure. A restart is not a crash loop.

    Only the lease holder may release: the ``workers`` row for
    ``installation_id`` must point at this job (``claim`` writes that), unless
    the lease has already lapsed, in which case anyone may tidy it.
    """
    job_id = _int(job_id, 0)
    if job_id <= 0:
        return {"jobId": job_id, "released": False, "reason": "bad_job_id"}
    row = fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if row is None:
        return {"jobId": job_id, "released": False, "reason": "unknown_job"}
    if str(row["status"]) not in ("claimed", "running"):
        return {"jobId": job_id, "released": False, "reason": f"job_is_{row['status']}"}

    key = str(installation_id or "").strip()[:80]
    held_by_caller = bool(key) and _registry_job_id(key) == job_id
    if not held_by_caller and not _is_expired(row["lease_expires_at"]):
        return {"jobId": job_id, "released": False, "reason": "held_by_another_worker"}

    now = utc_now()
    with transaction():
        execute(
            """
            UPDATE jobs
               SET status = 'pending', claimed_at = NULL,
                   lease_token_hash = NULL, lease_expires_at = NULL, updated_at = ?
             WHERE id = ? AND status IN ('claimed','running')
            """,
            (now, job_id),
        )
        execute(
            "UPDATE job_targets SET status = 'pending', started_at = NULL "
            "WHERE job_id = ? AND status = 'running'",
            (job_id,),
        )
        if key:
            execute(
                "UPDATE workers SET current_job_id = NULL, last_seen_at = ? "
                "WHERE worker_id = ? AND current_job_id = ?",
                (now, key, job_id),
            )
        _sync_page_status(job_id)
    return {"jobId": job_id, "released": True, "reason": "released"}


# ---------------------------------------------------------------------------
# product-wise re-scan (2026-09-09)
#
# "Click a product -> re-scan the pages that run it." The Products drawer's
# Re-track button already queued one page_scan job over product["pages"], but it
# swallowed the two things create_scan_job reports — pages already in the queue
# and pages with no Meta page id (145 of the v1 imports) — so a 12-page product
# could silently become a 7-page job. This function returns every bucket, so
# the caller can say "queued 7 · already queued 2 · unscannable 3".
# ---------------------------------------------------------------------------
def product_pages_for_rescan(product_id: Any) -> list[dict[str, Any]]:
    """Every advertiser page carrying an ad of this product, most active first.

    Same join as ``product_service.attach_advertiser_pages`` (ad_products ->
    ads -> pages), plus what a re-scan needs to know about each page: whether
    it has a numeric Meta page id (``scannable``) and how many ads are active.
    """
    rows = fetch_all(
        """
        SELECT p.id AS id, p.platform_page_id AS platform_page_id, p.name AS name,
               p.alias AS alias, p.url AS url,
               COUNT(DISTINCT CASE WHEN lower(COALESCE(a.status,'active')) = 'active'
                                   THEN a.id END)                         AS active_ads,
               COUNT(DISTINCT a.id)                                        AS total_ads
          FROM ad_products ap
          JOIN ads a   ON a.id = ap.ad_id
          JOIN pages p ON p.id = a.page_id
         WHERE ap.product_id = ?
         GROUP BY p.id
         ORDER BY active_ads DESC, total_ads DESC, p.id
        """,
        (_int(product_id, 0),),
    )
    pages: list[dict[str, Any]] = []
    for row in rows:
        platform = str(row["platform_page_id"] or "")
        pages.append(
            {
                "id": _int(row["id"]),
                "platformPageId": platform,
                "label": target_label(row["alias"], row["name"], platform),
                "activeAds": _int(row["active_ads"]),
                "totalAds": _int(row["total_ads"]),
                "scannable": bool(meta_ads_library_url(platform, row["url"])),
            }
        )
    return pages


def _pages_with_open_targets(page_ids: Sequence[int]) -> set[int]:
    ids = [int(p) for p in page_ids]
    if not ids:
        return set()
    rows = fetch_all(
        f"""
        SELECT DISTINCT t.page_id AS page_id
          FROM job_targets t JOIN jobs j ON j.id = t.job_id
         WHERE t.page_id IN ({_placeholders(ids)})
           AND j.status IN {JOB_ACTIVE_STATUSES}
           AND t.status IN {TARGET_ACTIVE_STATUSES}
        """,
        ids,
    )
    return {_int(r["page_id"]) for r in rows}


def rescan_product_pages(
    product_id: Any,
    *,
    include_inactive: bool = False,
    label: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Queue ONE page_scan job over the pages that run this product.

    Returns ``{productId, productName, jobId, created, queued, skipped,
    unscannable, excludedInactive, reason}``. ``jobId`` is ``None`` when nothing
    could be queued; ``reason`` then says why (``already_queued`` /
    ``no_meta_page_id`` / ``no_pages``). A ``duplicate`` reason carries the id of
    the job an earlier click created today.

    * default set = pages with at least one ACTIVE ad; ``include_inactive``
      adds the rest (a page whose ads all stopped is usually noise to re-scan);
    * pages already queued/running are skipped, not queued twice (PRD P0.2);
    * pages with no numeric Meta page id are reported, never queued (R5);
    * idempotent per product per UTC day (``product:<id>:<YYYY-MM-DD>``), so a
      double-click cannot create two jobs. A product re-scanned again after
      that job FINISHED gets a fresh key (``:2``, ``:3`` ...).
    """
    product = fetch_one(
        "SELECT id, display_name, normalized_name FROM products WHERE id = ?",
        (_int(product_id, 0),),
    )
    if product is None:
        raise NotFoundError(f"product {product_id} does not exist")
    product_name = str(product["display_name"] or product["normalized_name"] or "").strip()

    pages = product_pages_for_rescan(product["id"])
    chosen = [p for p in pages if include_inactive or p["activeAds"] > 0]
    excluded_inactive = len(pages) - len(chosen)
    unscannable = [p for p in chosen if not p["scannable"]]
    scannable = [p for p in chosen if p["scannable"]]
    busy = _pages_with_open_targets([p["id"] for p in scannable])
    skipped = [p for p in scannable if p["id"] in busy]
    queued = [p for p in scannable if p["id"] not in busy]

    result: dict[str, Any] = {
        "productId": _int(product["id"]),
        "productName": product_name,
        "jobId": None,
        "created": False,
        "queued": queued,
        "skipped": skipped,
        "unscannable": unscannable,
        "excludedInactive": excluded_inactive,
        "reason": "",
    }
    # Idempotency FIRST: the second click of a double-click finds every page
    # busy (the first click queued them), and reporting that as a generic
    # "already queued" hides the useful answer — "that is job #N from today".
    base_key = (idempotency_key or "").strip() or f"product:{product['id']}:{utc_now()[:10]}"
    key = base_key
    suffix = 1
    while True:
        existing = fetch_one("SELECT id, status FROM jobs WHERE idempotency_key = ?", (key,))
        if existing is None:
            break
        if str(existing["status"]) in JOB_ACTIVE_STATUSES:
            result.update(jobId=_int(existing["id"]), reason="duplicate",
                          queued=[], skipped=queued + skipped)
            return result
        suffix += 1
        key = f"{base_key}:{suffix}"

    if not queued:
        if skipped:
            result["reason"] = "already_queued"
        elif unscannable:
            result["reason"] = "no_meta_page_id"
        else:
            result["reason"] = "no_pages"
        return result

    job_label = (label or "").strip() or (
        f"Re-scan {product_name or 'product'} ({len(queued)} page{'s' if len(queued) != 1 else ''})"
    )
    try:
        job = create_job([p["id"] for p in queued], label=job_label, idempotency_key=key)
    except DuplicateJobError:
        # A worker claimed one of these pages between our clash check and the
        # insert. Report, never fall back to a second insert.
        result.update(reason="already_queued", queued=[], skipped=queued + skipped)
        return result
    result.update(jobId=_int(job["jobId"]), created=True)
    return result
