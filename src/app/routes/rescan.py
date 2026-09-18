"""Product-wise re-scan — "click a product, re-scan the pages that run it".

Three routes, one service function (``job_service.rescan_product_pages``):

    GET  /rescan/product/<id>/preview      JSON: the pages a re-scan would cover,
                                           bucketed (active / no active ads /
                                           unscannable / already queued) — for a
                                           drawer that wants to show "Re-scan this
                                           product's pages (N)" with page pills
                                           and a confirm above 20 pages.
    POST /rescan/product/<id>              form: queues ONE page_scan job, flashes
                                           "queued X · already queued Y · unscannable
                                           Z", redirects to ``next`` (same-origin
                                           path only) or the referrer.
                                           Fields: include_inactive=1 (optional),
                                           next=/products?... (optional)
    POST /api/rescan/product/<id>          JSON envelope of the same call
                                           ({"ok": true, "result": {...}}).
                                           Body: {"includeInactive": bool}

Deliberately OUTSIDE ``/api/worker`` and ``/api/jobs``: the worker protocol is
five endpoints and two tests ratchet that (test_worker_api /
test_end_to_end). This is a dashboard action, not something the extension
calls.

The drawer button in templates/products.html belongs to the products agent; it
currently posts to ``products.retrack``. Pointing it at
``url_for('rescan.rescan_product', product_id=product.id, next=back)`` gives it
the fuller flash and the per-day idempotency for free.
"""

from __future__ import annotations

import os
import secrets
from typing import Any

from flask import Blueprint, flash, jsonify, redirect, request, url_for

from .. import job_service

bp = Blueprint("rescan", __name__)

CONFIRM_ABOVE_PAGES = 20   # a 35-page job is 4-5 h at 20 pages/hour (job 4: 4h41m)


@bp.record_once
def _ensure_session_key(state) -> None:
    """flash() needs a session key; same guard as app/routes/queue.py."""
    app = state.app
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = os.environ.get("ADSPY2_SECRET_KEY") or secrets.token_hex(32)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _safe_next(candidate: str | None) -> str | None:
    """Only a same-origin path: ``/products?...``. Anything with a scheme or a
    ``//`` prefix is dropped so a crafted link cannot bounce the owner away."""
    text = str(candidate or "").strip()
    if text.startswith("/") and not text.startswith("//") and "\\" not in text:
        return text
    return None


def _back() -> str:
    for candidate in (request.form.get("next"), request.args.get("next"), request.referrer):
        safe = _safe_next(candidate)
        if safe:
            return safe
    return "/products"


def _names(pages: list[dict]) -> str:
    labels = [str(p.get("label") or p.get("platformPageId") or "page") for p in pages]
    shown = ", ".join(labels[:4])
    return shown + (f" +{len(labels) - 4} more" if len(labels) > 4 else "")


def _flash_result(result: dict[str, Any]) -> None:
    name = result.get("productName") or "this product"
    queued = result.get("queued") or []
    skipped = result.get("skipped") or []
    unscannable = result.get("unscannable") or []
    excluded = int(result.get("excludedInactive") or 0)
    reason = str(result.get("reason") or "")

    if result.get("jobId") and result.get("created"):
        ids = ", ".join(f"#{i}" for i in result.get("jobIds") or [result["jobId"]])
        flash(
            f"Job(s) {ids} queued: {len(queued)} page(s) running {name} — "
            f"{_names(queued)}.",
            "success",
        )
    elif reason == "duplicate":
        flash(
            f"Job #{result['jobId']} for {name} is already in the queue from earlier today — "
            "not queuing it twice.",
            "warning",
        )
    elif reason == "already_queued":
        ids = result.get("jobIds") or []
        if ids:
            names = ", ".join(f"#{i}" for i in ids)
            flash(
                f"The pages running {name} are already covered by job(s) {names} — "
                "not queuing them twice.",
                "warning",
            )
        else:
            flash(f"The pages running {name} are already queued or running.", "warning")
    elif reason == "no_meta_page_id":
        pass   # the sentence below says it better
    else:
        flash(f"No advertiser pages with active ads to re-scan for {name}.", "warning")

    if result.get("created") and skipped:
        flash(f"Already in the queue, skipped: {_names(skipped)}.", "warning")
    if unscannable:
        flash(
            f"Can't scan {_names(unscannable)}: no Meta page id. Open the page in the Ad "
            "Library and add it again from that URL (the one with view_all_page_id=...).",
            "warning",
        )
    if excluded:
        flash(
            f"{excluded} page(s) with no active ads left out — tick "
            "\"include pages with no active ads\" to scan them too.",
            "info",
        )


def _preview(product_id: int) -> dict[str, Any]:
    if job_service.fetch_one("SELECT 1 FROM products WHERE id = ?", (int(product_id),)) is None:
        raise job_service.NotFoundError(f"product {product_id} does not exist")
    pages = job_service.product_pages_for_rescan(product_id)
    busy = job_service._pages_with_open_targets([p["id"] for p in pages])  # noqa: SLF001
    for page in pages:
        page["queued"] = page["id"] in busy
    active = [p for p in pages if p["activeAds"] > 0]
    default_set = [p for p in active if p["scannable"] and not p["queued"]]
    return {
        "productId": int(product_id),
        "pages": pages,
        "counts": {
            "total": len(pages),
            "active": len(active),
            "inactiveOnly": len(pages) - len(active),
            "unscannable": sum(1 for p in pages if not p["scannable"]),
            "queued": sum(1 for p in pages if p["queued"]),
            "defaultSet": len(default_set),
        },
        "confirmAbove": CONFIRM_ABOVE_PAGES,
        "needsConfirm": len(default_set) > CONFIRM_ABOVE_PAGES,
    }


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@bp.get("/rescan/product/<int:product_id>/preview")
def rescan_preview(product_id: int):
    try:
        return jsonify({"ok": True, "result": _preview(product_id)})
    except job_service.JobError as exc:
        return jsonify({"ok": False, "error": exc.message, "code": exc.code}), exc.status


@bp.post("/rescan/product/<int:product_id>")
def rescan_product(product_id: int):
    include_inactive = _truthy(request.form.get("include_inactive"))
    try:
        result = job_service.rescan_product_pages(product_id, include_inactive=include_inactive)
    except job_service.NotFoundError:
        flash("That product no longer exists.", "error")
        return redirect(_back())
    except job_service.JobError as exc:
        flash(f"Could not queue a re-scan: {exc.message}", "error")
        return redirect(_back())
    _flash_result(result)
    if result.get("jobId") and result.get("created"):
        # Back to where the owner was (the drawer carries `next`), with the job
        # id in the query string so the page can offer "open job #id".
        back = _back()
        joiner = "&" if "?" in back else "?"
        return redirect(f"{back}{joiner}job={int(result['jobId'])}")
    return redirect(_back())


@bp.post("/api/rescan/product/<int:product_id>")
def rescan_product_api(product_id: int):
    payload = request.get_json(silent=True) or {}
    include_inactive = bool(payload.get("includeInactive")) or _truthy(
        request.args.get("include_inactive")
    )
    try:
        result = job_service.rescan_product_pages(product_id, include_inactive=include_inactive)
    except job_service.JobError as exc:
        return jsonify({"ok": False, "error": exc.message, "code": exc.code}), exc.status
    if result.get("jobId"):
        result["jobUrl"] = url_for("queue.job_detail", job_id=int(result["jobId"]))
    return jsonify({"ok": True, "result": result})


__all__ = ["bp"]
