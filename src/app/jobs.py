"""Worker API — the five endpoints the Chrome extension talks to.

Normative contract: docs/04-extension-spec.md §4 (docs/03-architecture.md §4 is
the same table; 04 wins on any disagreement). v1 had sixteen worker endpoints
plus a worker registry, a session table and a two-token bootstrap dance. That
machinery is gone. What is left:

    POST /api/worker/hello          register + session + heartbeat, in one
                                    (+ since 2026-09-09: the dashboard's live
                                    job truth for the panel's Refresh, and an
                                    optional ``releaseJobId`` for a clean
                                    resume after a Chrome restart — additive)
    POST /api/worker/claim          one job, one lease
    POST /api/jobs/<id>/batch       ads in, idempotent by batchId
    POST /api/jobs/<id>/status      per-target live progress
    POST /api/jobs/<id>/done        terminal outcome for the job

Envelope on every response: ``{"ok": true, "result": {...}}`` or
``{"ok": false, "error": "...", "code": "..."}``.

Auth is one shared token (``job_service.worker_token()``) sent as
``X-Worker-Token``; per-job calls also send ``X-Lease-Token``. No login, no
users, no worker registry — the app binds 127.0.0.1 and that is the boundary
(docs/00-decisions.md #4).

Two blueprints live here because the paths do: the contract puts hello/claim
under ``/api/worker`` and the per-job calls under ``/api/jobs/<id>``. Both are
picked up automatically by ``create_app``'s blueprint discovery.
"""

from __future__ import annotations

import importlib
import json
import logging
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from . import config as app_config
from . import job_service
from .db import execute, fetch_one, transaction
from .job_service import AuthError, JobError
from .time_utils import utc_now

log = logging.getLogger("adspy2.jobs")

bp = Blueprint("jobs", __name__, url_prefix="/api/worker")
job_bp = Blueprint("jobs_api", __name__, url_prefix="/api/jobs")

WORKER_TOKEN_HEADER = "X-Worker-Token"
LEASE_TOKEN_HEADER = "X-Lease-Token"


# ---------------------------------------------------------------------------
# envelope + input helpers
# ---------------------------------------------------------------------------
def _ok(result: Any = None, status: int = 200):
    return jsonify({"ok": True, "result": result if result is not None else {}}), status


def _error(message: str, code: str, status: int):
    return jsonify({"ok": False, "error": message, "code": code}), status


def _payload() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    return str(value or "").strip()


def _require_worker_token() -> None:
    if not job_service.verify_worker_token(request.headers.get(WORKER_TOKEN_HEADER, "")):
        raise AuthError(
            "missing or invalid worker token — copy it from the Queue screen",
            code="BAD_WORKER_TOKEN",
        )


def _trusted_first_contact() -> bool:
    """docs/04 §4: the very first /hello may be tokenless, but only from the
    extension itself on loopback. Everything after that carries the token.

    SERVER MODE: never. Behind a reverse proxy every request reaches gunicorn
    from loopback, so ``remote_addr`` proves nothing and a tokenless hello would
    HAND the worker token to any stranger on the internet. There the token is
    pasted by the owner, never issued (app/auth.py gates the same thing)."""
    if current_app.config.get("SERVER_MODE"):
        return False
    if request.remote_addr not in (None, "", "127.0.0.1", "::1", "localhost"):
        return False
    origin = _text(request.headers.get("Origin"))
    if not origin:
        return True
    return origin.startswith(
        ("chrome-extension://", "moz-extension://", "http://127.0.0.1", "http://localhost")
    )


for _blueprint in (bp, job_bp):

    @_blueprint.errorhandler(JobError)
    def _handle_job_error(exc: JobError):  # noqa: ANN001 - flask handler
        return _error(exc.message, exc.code, exc.status)


# ---------------------------------------------------------------------------
# 1. POST /api/worker/hello
# ---------------------------------------------------------------------------
@bp.post("/hello")
def hello():
    """Register + session + heartbeat in one call. Returns the shared worker
    token so the extension can confirm what the owner pasted is still current."""
    data = _payload()
    supplied = _text(request.headers.get(WORKER_TOKEN_HEADER))
    if supplied:
        _require_worker_token()
    elif not _trusted_first_contact():
        raise AuthError(
            "worker token required", code="BAD_WORKER_TOKEN"
        )

    job_service.reap_expired_leases()
    current_job_id = data.get("currentJobId")
    installation_id = _text(data.get("installationId"))

    # D7. Clean resume. The extension keeps its lease token in storage.session
    # (R10), which a Chrome restart wipes; it still knows the job id it held
    # and says so here. Releasing puts the job straight back to pending WITHOUT
    # burning a retry — before this, three restarts on one long job were a
    # permanent LEASE_EXPIRED. Only the lease holder may release (checked in
    # job_service.release_lease), and it runs BEFORE the registry upsert so
    # the holder check sees the claim-time `workers.current_job_id`.
    released = None
    release_job_id = _int(data.get("releaseJobId"), 0)
    if release_job_id > 0:
        released = job_service.release_lease(release_job_id, installation_id)
        log.info("worker %s asked to release job %s: %s",
                 installation_id or "?", release_job_id, released.get("reason"))

    # The job this worker holds under a live lease, read BEFORE the upsert so a
    # hello that carries no currentJobId (an idle beat after a service-worker
    # restart) can still be told about it from the registry — and the registry
    # row keeps pointing at that job instead of being blanked by this beat.
    active_job = None
    try:
        active_job = job_service.active_job_for_worker(installation_id, current_job_id)
    except Exception:  # noqa: BLE001 - bookkeeping never breaks a heartbeat
        log.exception("active_job_for_worker failed for %s", installation_id or "?")
    if active_job and not _int(current_job_id, 0):
        current_job_id = active_job.get("jobId")

    # D1. Leave a trace that this worker is alive. The `workers` table shipped
    # in migration 002 and nothing had ever written a row to it, so the
    # dashboard could not distinguish "the extension halted on a captcha three
    # hours ago" from "Chrome is closed" — which is exactly the state the owner
    # reported as "the plugin is not working any more". `haltReason` is what a
    # halted worker sends instead of going silent (bg/orchestrator.js D2).
    job_service.upsert_worker(
        data.get("installationId"),
        state=_text(data.get("state")) or "idle",
        extension_version=_text(data.get("extensionVersion")),
        current_job_id=current_job_id,
        last_error=_text(data.get("haltReason")) or _text(data.get("lastError")),
    )

    return _ok(
        {
            "workerToken": job_service.worker_token(),
            "command": job_service.command_for(current_job_id),
            "queuedJobs": job_service.queued_job_count(),
            "serverTime": utc_now(),
            "leaseSeconds": int(app_config.LEASE_TTL_SECONDS),
            "helloIntervalSeconds": int(app_config.WORKER_HEARTBEAT_SECONDS),
            "installationId": _text(data.get("installationId")),
            "extensionVersion": _text(data.get("extensionVersion")),
            "dataset": _dataset_summary(),
            # --- additive since 2026-09-09 (extension REFRESH) -------------
            # `activeJob`: job_status_report of the job THIS worker holds under
            # a live lease, else null — the panel rebuilds its queue rows from
            # it, so rows from a dead job can no longer linger for 12 days.
            # `liveJobIds` / `liveJobs`: everything pending/claimed/running, so
            # the panel can show the dashboard queue while idle and drop local
            # state for a job the dashboard no longer knows.
            "activeJob": active_job,
            "liveJobIds": _safe_list(job_service.live_job_ids),
            "liveJobs": _safe_list(job_service.live_jobs_summary),
            "released": released,
        }
    )


def _safe_list(reader) -> list:
    """A hello must never fail on a read that only feeds the panel."""
    try:
        return list(reader() or [])
    except Exception:  # noqa: BLE001
        log.exception("hello extra read failed: %s", getattr(reader, "__name__", reader))
        return []


def _dataset_summary() -> dict[str, Any]:
    """Which dataset scans land in (app/dataset.py). The panel may render it
    under the connection dot; the extension needs nothing else to route."""
    from . import dataset

    info = dataset.describe()
    return {"active": info["name"], "label": info["label"],
            "scanAllowed": not info["frozen"]}


# ---------------------------------------------------------------------------
# 2. POST /api/worker/claim
# ---------------------------------------------------------------------------
@bp.post("/claim")
def claim():
    """Hand out exactly one job with its ordered targets, under a lease. Typed
    refusals instead of an empty 204 so the panel can say *why* it is idle."""
    _require_worker_token()
    data = _payload()
    installation_id = _text(data.get("installationId"))
    if not job_service.scan_writes_allowed():
        # OLD DATA is active (app/dataset.py): idle, do not error-storm.
        result: dict[str, Any] = {"job": None, "reason": "dataset_frozen"}
    else:
        result = job_service.claim_next_job(installation_id)
    job_service.upsert_worker(
        installation_id,
        state="running" if result.get("job") else "claiming",
        extension_version=_text(data.get("extensionVersion")),
        current_job_id=(result.get("job") or {}).get("jobId"),
    )

    if result.get("job") is None:
        return _ok(
            {
                "job": None,
                "reason": result.get("reason", "queue_empty"),
                "queuedJobs": job_service.queued_job_count(),
                "retryAfterSeconds": 15,
            }
        )
    return _ok({"job": result["job"], "leaseToken": result["leaseToken"]})


# ---------------------------------------------------------------------------
# 3. POST /api/jobs/<job_id>/batch
# ---------------------------------------------------------------------------
@job_bp.post("/<int:job_id>/batch")
def batch(job_id: int):
    """Ads in. Idempotent by ``(job_id, batchId)``: a replayed parked batch (R9)
    gets the original receipt back verbatim and ingest never runs twice."""
    _require_worker_token()
    job = job_service.validate_lease(job_id, request.headers.get(LEASE_TOKEN_HEADER, ""))

    payload = _payload()
    batch_id = _text(payload.get("batchId"))
    if not batch_id:
        raise JobError("batchId is required (it is the idempotency key)",
                       code="BATCH_ID_REQUIRED")

    # THE FREEZE: refused before the receipt lookup so nothing is written and
    # nothing is replayed while OLD DATA is active (ingest checks again).
    job_service.assert_scan_writes_allowed()

    stored = _stored_receipt(job_id, batch_id)
    if stored is not None:
        job_service.renew_lease(job_id)
        stored["duplicate"] = True
        # A4. A replay is a real answer to a real POST and must have the SAME
        # SHAPE as a first delivery, because the worker reads two fields off it
        # and only ever reads them here.
        #
        # `command` is how the dashboard's Cancel reaches a running worker. The
        # stored receipt is the one app/ingest.py wrote, and it carries neither
        # field, so while batcher.drainPending() replays a parked batch ring
        # (R9 — up to 40 payloads) every reply said "no command": a cancel
        # pressed during a drain was silently dropped for the whole drain.
        # `accepted` is what the batcher checks before it retires a parked
        # payload; absent, it reads as falsy.
        #
        # Recomputed, never read from the stored blob: the job's state now is
        # what matters, not what it was when the batch first landed.
        stored["accepted"] = stored.get("status") != "rejected"
        stored["command"] = job_service.command_for_running_job(job_id)
        return _ok(stored)

    normalized, warnings = _normalize_batch(payload, job_type=str(job["job_type"]))
    ingest = _ingest_module()

    try:
        result = ingest.ingest_batch(job_id, normalized) or {}
    except JobError:
        raise
    except Exception as exc:  # ingest rejects a bad batch by raising
        code = getattr(exc, "code", "") or "BATCH_REJECTED"
        log.warning("ingest rejected batch %s of job %s: %r", batch_id, job_id, exc)
        raise JobError(str(exc) or "batch rejected", code=str(code), status=400) from exc

    receipt = _build_receipt(job_id, batch_id, normalized, result, warnings)
    _persist_receipt(job_id, batch_id, normalized, result, receipt)

    job_service.record_batch_progress(
        job_id,
        position=normalized.get("targetPosition"),
        page_id=result.get("pageId"),
        # A3: the target's running total when the worker sends one (it does
        # since 2026-08-16); the per-batch sum is the pre-fix fallback.
        represented_ads=payload.get("representedAdTotal", payload.get("representedAdCount")),
        estimated_results=payload.get("estimatedResults"),
        unique_ads=len(receipt["acceptedAdLibraryIds"]),
    )
    job_service.renew_lease(job_id)
    receipt["command"] = job_service.command_for_running_job(job_id)
    return _ok(receipt)


def _ingest_module():
    """Imported lazily so the app still boots (and /hello still answers) while
    app/ingest.py is being written."""
    try:
        return importlib.import_module("app.ingest")
    except ImportError as exc:  # pragma: no cover - only before Phase 1 lands
        raise JobError(
            "ingest module is not available", code="INGEST_UNAVAILABLE", status=503
        ) from exc


def _normalize_batch(payload: dict[str, Any], *, job_type: str) -> tuple[dict[str, Any], list[str]]:
    """Server-side enforcement of R4 before ingest ever sees the batch.

    ``isFinal`` triggers reconciliation, and reconciliation deactivates ads. A
    worker that reports ``isFinal`` alongside ``partial``/``blocked``/``failed``
    is claiming a scan finished when it did not — we strip the flag rather than
    trust it, and say so in the receipt's warnings.
    """
    warnings: list[str] = []
    batch = dict(payload)

    outcome = _text(batch.get("outcome")).lower() or None
    if outcome and outcome not in job_service.VALID_OUTCOMES:
        warnings.append(f"unknown outcome {outcome!r} ignored")
        outcome = None

    is_final = bool(batch.get("isFinal"))
    if is_final and outcome not in job_service.FINAL_OUTCOMES:
        warnings.append(
            f"isFinal dropped: outcome {outcome or 'missing'!r} is not one of "
            f"{list(job_service.FINAL_OUTCOMES)} (R4) — no reconciliation"
        )
        is_final = False

    batch["outcome"] = outcome
    batch["isFinal"] = is_final
    batch["jobType"] = job_type
    batch["searchType"] = "page" if job_type == "page_scan" else "keyword_unordered"
    batch["batchId"] = _text(batch.get("batchId"))
    batch["batchSequence"] = _int(batch.get("batchSequence"))
    batch["targetPosition"] = _int(batch.get("targetPosition"))
    if not isinstance(batch.get("ads"), list):
        batch["ads"] = []
    return batch, warnings


def _stored_receipt(job_id: int, batch_id: str) -> dict[str, Any] | None:
    row = fetch_one(
        "SELECT receipt_json, accepted_ad_ids_json FROM job_batches "
        "WHERE job_id = ? AND batch_id = ? AND status = 'accepted'",
        (int(job_id), batch_id),
    )
    if row is None:
        return None
    receipt = _loads(row["receipt_json"], {})
    if not isinstance(receipt, dict) or not receipt:
        receipt = {
            "accepted": True,
            "acceptedAdLibraryIds": _loads(row["accepted_ad_ids_json"], []),
        }
    receipt.setdefault("acceptedAdLibraryIds", _loads(row["accepted_ad_ids_json"], []))
    receipt["batchId"] = batch_id
    return receipt


def _build_receipt(
    job_id: int,
    batch_id: str,
    batch: dict[str, Any],
    result: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    accepted_ids = result.get("acceptedAdLibraryIds")
    if accepted_ids is None:
        row = fetch_one(
            "SELECT accepted_ad_ids_json FROM job_batches WHERE job_id = ? AND batch_id = ?",
            (int(job_id), batch_id),
        )
        accepted_ids = _loads(row["accepted_ad_ids_json"], []) if row else []
    status = _text(result.get("status")) or "accepted"

    extra = result.get("warnings") or []
    return {
        "accepted": status != "rejected",
        "duplicate": status == "duplicate",
        "batchId": batch_id,
        "acceptedAdLibraryIds": [str(x) for x in accepted_ids],
        "adsSeen": _int(result.get("adsSeen")),
        "adsNew": _int(result.get("adsNew")),
        "adsUpdated": _int(result.get("adsUpdated")),
        "adsDeactivated": _int(result.get("adsDeactivated")),
        "pageId": result.get("pageId"),
        "isFinal": bool(batch.get("isFinal")),
        "warnings": [*warnings, *[str(w) for w in extra]],
    }


def _persist_receipt(
    job_id: int,
    batch_id: str,
    batch: dict[str, Any],
    result: dict[str, Any],
    receipt: dict[str, Any],
) -> None:
    """``ingest_batch`` normally writes the ``job_batches`` row itself (it needs
    the accepted-id union for reconciliation). If it did not, the endpoint still
    guarantees the contract: one row per (job, batchId), receipt replayable."""
    row = fetch_one(
        "SELECT id, receipt_json FROM job_batches WHERE job_id = ? AND batch_id = ?",
        (int(job_id), batch_id),
    )
    receipt_json = json.dumps(receipt, ensure_ascii=False)
    with transaction():
        if row is None:
            execute(
                """
                INSERT INTO job_batches(job_id, batch_id, batch_sequence, target_position,
                                        page_id, is_final, outcome, status, ads_seen,
                                        ads_new, ads_updated, ads_deactivated,
                                        represented_ad_count, accepted_ad_ids_json,
                                        receipt_json, received_at)
                VALUES(?,?,?,?,?,?,?,'accepted',?,?,?,?,?,?,?,?)
                """,
                (
                    int(job_id),
                    batch_id,
                    _int(batch.get("batchSequence")),
                    _int(batch.get("targetPosition")),
                    result.get("pageId"),
                    1 if batch.get("isFinal") else 0,
                    batch.get("outcome"),
                    _int(result.get("adsSeen")),
                    _int(result.get("adsNew")),
                    _int(result.get("adsUpdated")),
                    _int(result.get("adsDeactivated")),
                    _int(batch.get("representedAdCount")),
                    json.dumps(receipt["acceptedAdLibraryIds"]),
                    receipt_json,
                    utc_now(),
                ),
            )
        elif not _loads(row["receipt_json"], {}):
            execute(
                "UPDATE job_batches SET receipt_json = ? WHERE id = ?",
                (receipt_json, int(row["id"])),
            )


def _loads(raw: Any, default: Any) -> Any:
    try:
        value = json.loads(raw or "null")
    except (TypeError, ValueError):
        return default
    return default if value is None else value


# ---------------------------------------------------------------------------
# 4. POST /api/jobs/<job_id>/status
# ---------------------------------------------------------------------------
@job_bp.post("/<int:job_id>/status")
def status(job_id: int):
    """Per-target live progress — this is the dashboard's live row, and it is
    also the heartbeat while a job is running."""
    _require_worker_token()
    job_service.validate_lease(job_id, request.headers.get(LEASE_TOKEN_HEADER, ""))
    payload = _payload()

    result = job_service.record_target_progress(
        job_id,
        position=payload.get("targetPosition"),
        state=_text(payload.get("state")).lower(),
        outcome=payload.get("outcome"),
        scrolls=payload.get("scrolls"),
        unique_ads=payload.get("uniqueAds"),
        represented_ads=payload.get("representedAds"),
        message=_text(payload.get("message")),
    )
    return _ok(result)


# ---------------------------------------------------------------------------
# 5. POST /api/jobs/<job_id>/done
# ---------------------------------------------------------------------------
@job_bp.post("/<int:job_id>/done")
def done(job_id: int):
    """Terminal outcome for the whole job. ``retryable: true`` with retries left
    returns the job — and its unfinished targets — to pending."""
    _require_worker_token()
    job_service.validate_lease(job_id, request.headers.get(LEASE_TOKEN_HEADER, ""))
    payload = _payload()

    result = job_service.finish_job(
        job_id,
        outcome=_text(payload.get("outcome")).lower(),
        error=_text(payload.get("error")),
        error_code=_text(payload.get("code")),
        retryable=bool(payload.get("retryable")),
    )
    return _ok(result)


__all__ = ["bp", "job_bp"]
