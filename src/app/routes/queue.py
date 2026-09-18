"""Queue screen — jobs, per-target live status, retry, errors.

This module also owns the *creation* of page-scan jobs, because the pages
screen's "Re-track" button needs it and Phase 2's worker API only ever
*consumes* jobs. If a sibling module ships a richer job service
(``app.job_service.create_job``) we delegate to it; otherwise the local
implementation below is the whole thing — an INSERT into ``jobs`` plus one
``job_targets`` row per page, which is exactly what ``/api/worker/claim``
reads.

The one product rule here (PRD P0.2): a page that is already sitting in a
pending/claimed/running job is never queued a second time. Duplicate scans of
the same page in the same hour are the fastest way to get an FB account
flagged.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import os
import secrets
from typing import Sequence

from flask import Blueprint, flash, redirect, render_template, request, url_for

from .. import db, queries
from ..time_utils import utc_now

log = logging.getLogger("adspy2.queue")

bp = Blueprint("queue", __name__)

RETRYABLE_STATES = ("failed", "cancelled", "completed")


@bp.record_once
def _ensure_session_key(state) -> None:
    """Same guard as app/routes/pages.py — flash() needs a session key, and this
    blueprint must still work if the pages module ever fails to import."""
    app = state.app
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = (
            os.environ.get("ADSPY2_SECRET_KEY") or secrets.token_hex(32)
        )


# ---------------------------------------------------------------------------
# job creation (used by the pages screen)
# ---------------------------------------------------------------------------
def service():
    """``app.job_service`` when it is importable, else None.

    Phase 2 owns that module; this screen is written so it still works without
    it (the local paths below do the same INSERTs). Everything is wrapped
    because a broken sibling should degrade the Re-track button, not 500 it.
    """
    try:
        return importlib.import_module("app.job_service")
    except ImportError:  # pragma: no cover - only before Phase 2 lands
        return None


def _job_id_of(result: object) -> int | None:
    if isinstance(result, int):
        return result
    if isinstance(result, dict):
        for key in ("job_id", "jobId", "id"):
            if key in result:
                return int(result[key])
    job_id = getattr(result, "id", None)
    return int(job_id) if job_id is not None else None


def _delegate_create_job(page_ids: Sequence[int], label: str) -> tuple[int | None, str]:
    """(job_id, outcome) where outcome is 'created', 'refused' or 'absent'.

    'refused' matters: the service raising DuplicateJobError means a worker
    claimed one of these pages between our clash check and the insert. Falling
    back to the local INSERT then would create exactly the duplicate scan the
    service just refused, so the caller reports it instead.
    """
    module = service()
    create = getattr(module, "create_job", None) if module else None
    if not callable(create):
        return None, "absent"
    try:
        return _job_id_of(create(page_ids=list(page_ids), label=label)), "created"
    except Exception as exc:  # pragma: no cover - race / bad input
        log.warning("job_service.create_job refused: %r", exc)
        return None, "refused"


def create_scan_job(page_ids: Sequence[int], label: str | None = None) -> dict:
    """Queue a page_scan job for these pages, skipping already-queued ones.

    Returns ``{job_id, queued: [page dicts], skipped: [page dicts], reason}``.
    ``job_id`` is None when nothing was left to queue.
    """
    wanted = []
    for value in page_ids:
        try:
            page_id = int(value)
        except (TypeError, ValueError):
            continue
        if page_id not in wanted:
            wanted.append(page_id)

    pages = queries.pages_for_targets(wanted)
    if not pages:
        return {"job_id": None, "queued": [], "skipped": [],
                "unscannable": [], "reason": "no_pages"}

    # A page with no numeric Meta page id has no Ad Library URL to open, so the
    # worker would sit on a page that never renders and eventually report a
    # block (see job_service.UnscannablePageError). Filter those out here rather
    # than let create_job refuse the whole selection — one bad page in a bulk
    # re-track should not stop the other forty.
    unscannable = [p for p in pages if not p.get("library_url")]
    scannable = [p for p in pages if p.get("library_url")]
    if not scannable:
        return {
            "job_id": None, "queued": [], "skipped": pages,
            "unscannable": unscannable, "reason": "no_meta_page_id",
        }

    busy = queries.pages_with_open_targets([p["id"] for p in scannable])
    queued = [p for p in scannable if int(p["id"]) not in busy]
    skipped = [p for p in scannable if int(p["id"]) in busy]
    if not queued:
        return {
            "job_id": None, "queued": [], "skipped": skipped,
            "unscannable": unscannable, "reason": "already_queued",
        }

    job_label = (label or "").strip() or (
        queued[0]["display_name"] if len(queued) == 1 else f"Re-track {len(queued)} pages"
    )

    delegated, outcome = _delegate_create_job([p["id"] for p in queued], job_label)
    if outcome == "created":
        return {"job_id": delegated, "queued": queued, "skipped": skipped,
                "unscannable": unscannable, "reason": ""}
    if outcome == "refused":
        return {
            "job_id": None,
            "queued": [],
            "skipped": queued + skipped,
            "unscannable": unscannable,
            "reason": "already_queued",
        }

    now = utc_now()
    fingerprint = hashlib.sha1(
        ("|".join(str(p["id"]) for p in queued) + "@" + now).encode("utf-8")
    ).hexdigest()[:16]

    with db.transaction():
        cursor = db.execute(
            """
            INSERT INTO jobs (job_type, status, idempotency_key, label,
                              targets_total, targets_done, created_at, updated_at)
            VALUES ('page_scan', 'pending', ?, ?, ?, 0, ?, ?)
            """,
            (f"ui:{fingerprint}", job_label, len(queued), now, now),
        )
        job_id = int(cursor.lastrowid)

        for position, page in enumerate(queued, start=1):
            db.execute(
                """
                INSERT INTO job_targets (job_id, position, page_id, platform_page_id,
                                         page_url, label, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    job_id,
                    position,
                    int(page["id"]),
                    str(page.get("platform_page_id") or ""),
                    page.get("library_url") or page.get("url") or "",
                    page.get("display_name") or page.get("name") or "",
                ),
            )

        db.execute(
            f"""
            UPDATE pages SET current_scan_status = 'queued', updated_at = ?
            WHERE id IN ({','.join('?' for _ in queued)})
            """,
            (now, *[int(p["id"]) for p in queued]),
        )

    return {"job_id": job_id, "queued": queued, "skipped": skipped,
            "unscannable": unscannable, "reason": ""}


# ---------------------------------------------------------------------------
# screens
# ---------------------------------------------------------------------------
@bp.get("/queue")
def queue_index():
    status = request.args.get("status", "")
    jobs = queries.list_jobs(status=status, limit=60)
    open_job_ids = [j["id"] for j in jobs if j["is_open"]]
    targets = {job_id: queries.job_targets(job_id) for job_id in open_job_ids}
    return render_template(
        "queue.html",
        title="Queue",
        active_nav="queue",
        jobs=jobs,
        targets=targets,
        counts=queries.queue_counts(),
        status=status,
        paused=queue_is_paused(),
    )


@bp.get("/queue/<int:job_id>")
def job_detail(job_id: int):
    job = queries.get_job(job_id)
    if job is None:
        return render_template("base.html", title="Job not found", not_found=True), 404
    return render_template(
        "queue.html",
        title=f"Job #{job_id}",
        active_nav="queue",
        jobs=[job],
        targets={job_id: queries.job_targets(job_id)},
        batches=queries.job_batches(job_id),
        counts=queries.queue_counts(),
        single=True,
        status="",
    )


@bp.get("/ui/queue/rows")
def queue_rows_fragment():
    """HTML fragment polled by static/app.js — the live per-target status.

    A fragment rather than JSON on purpose: the row markup stays in Jinja
    (docs/05: no HTML built outside templates), and the poll is one request
    even for a queue with twenty running targets.
    """
    job_id = request.args.get("job", type=int)
    if job_id:
        jobs = [j for j in [queries.get_job(job_id)] if j]
    else:
        jobs = queries.list_jobs(status=request.args.get("status", ""), limit=60)
    targets = {j["id"]: queries.job_targets(j["id"]) for j in jobs if j["is_open"]}
    return render_template("queue.html", fragment="rows", jobs=jobs, targets=targets)


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------
@bp.post("/queue/<int:job_id>/retry")
def retry_job(job_id: int):
    job = queries.get_job(job_id)
    if job is None:
        flash(f"Job #{job_id} does not exist.", "error")
        return redirect(url_for("queue.queue_index"))
    if job["status"] not in RETRYABLE_STATES:
        flash(f"Job #{job_id} is {job['status']} — let it finish first.", "warning")
        return redirect(url_for("queue.queue_index"))

    module = service()
    if module is not None and hasattr(module, "retry_job"):
        try:
            module.retry_job(job_id)
            flash(f"Job #{job_id} queued again.", "success")
        except Exception as exc:  # pragma: no cover - race with a claim
            flash(f"Could not retry job #{job_id}: {exc}", "error")
        return redirect(request.form.get("next") or url_for("queue.queue_index"))

    now = utc_now()
    with db.transaction():
        db.execute(
            """
            UPDATE jobs
               SET status = 'pending', outcome = NULL, error = NULL,
                   error_code = NULL, lease_token_hash = NULL,
                   lease_expires_at = NULL, claimed_at = NULL, finished_at = NULL,
                   cancel_requested_at = NULL, retry_count = retry_count + 1,
                   updated_at = ?
             WHERE id = ?
            """,
            (now, job_id),
        )
        db.execute(
            """
            UPDATE job_targets
               SET status = 'pending', outcome = NULL, message = NULL,
                   started_at = NULL, finished_at = NULL
             WHERE job_id = ? AND status IN ('running', 'failed', 'pending')
            """,
            (job_id,),
        )
    flash(f"Job #{job_id} queued again.", "success")
    return redirect(request.form.get("next") or url_for("queue.queue_index"))


@bp.post("/queue/<int:job_id>/cancel")
def cancel_job(job_id: int):
    job = queries.get_job(job_id)
    if job is None:
        flash(f"Job #{job_id} does not exist.", "error")
        return redirect(url_for("queue.queue_index"))

    module = service()
    if module is not None and hasattr(module, "cancel_job"):
        try:
            module.cancel_job(job_id)
            flash(
                f"Job #{job_id} cancelled."
                if job["status"] == "pending"
                else f"Cancel requested — job #{job_id} stops after the current page.",
                "success",
            )
        except Exception as exc:  # pragma: no cover
            flash(f"Could not cancel job #{job_id}: {exc}", "error")
        return redirect(request.form.get("next") or url_for("queue.queue_index"))

    now = utc_now()
    with db.transaction():
        if job["status"] == "pending":
            # Nobody holds a lease, so we can stop it outright.
            db.execute(
                """
                UPDATE jobs SET status = 'cancelled', cancel_requested_at = ?,
                                finished_at = ?, updated_at = ?
                 WHERE id = ?
                """,
                (now, now, now, job_id),
            )
            db.execute(
                "UPDATE job_targets SET status = 'skipped' WHERE job_id = ? AND status = 'pending'",
                (job_id,),
            )
            _reset_page_status(job_id, now)
            flash(f"Job #{job_id} cancelled.", "success")
        elif job["is_open"]:
            # A worker is on it: flag the request, the next /status call picks
            # up the cancel command (docs/04 §4).
            db.execute(
                "UPDATE jobs SET cancel_requested_at = ?, updated_at = ? WHERE id = ?",
                (now, now, job_id),
            )
            flash(f"Cancel requested — job #{job_id} stops after the current page.", "success")
        else:
            flash(f"Job #{job_id} already finished.", "warning")
    return redirect(request.form.get("next") or url_for("queue.queue_index"))


@bp.post("/queue/pause")
def toggle_pause():
    """Stop handing jobs out without cancelling anything.

    ``/api/worker/hello`` answers ``command: pause`` while this is set, so the
    extension parks itself. The owner needs this when Facebook starts looking
    at them funny (docs/04 R8) — the alternative is cancelling jobs one by one.
    """
    module = service()
    if module is None or not hasattr(module, "set_paused"):
        flash("Pause needs the worker service (app/job_service.py).", "error")
        return redirect(url_for("queue.queue_index"))
    paused = module.set_paused(request.form.get("paused") == "1")
    flash("Queue paused — the extension will idle." if paused else "Queue resumed.", "success")
    return redirect(request.form.get("next") or url_for("queue.queue_index"))


def queue_is_paused() -> bool:
    module = service()
    try:
        return bool(module.is_paused()) if module else False
    except Exception:  # pragma: no cover
        return False


def _reset_page_status(job_id: int, now: str) -> None:
    db.execute(
        """
        UPDATE pages SET current_scan_status = 'idle', updated_at = ?
         WHERE current_scan_status IN ('queued', 'running')
           AND id IN (SELECT page_id FROM job_targets
                       WHERE job_id = ? AND page_id IS NOT NULL)
        """,
        (now, job_id),
    )


__all__ = ["bp", "create_scan_job"]
