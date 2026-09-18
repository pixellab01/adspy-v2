"""Pages screens — the home screen and the page detail.

This is the screen the owner opens every morning, so it answers one question
before anything else: **what changed since the last scan?** That delta column
is the whole of v2's "daily news" (docs/00-decisions.md #6) — it replaces v1's
alerts tab, page monitors and trends subsystems, which between them produced
1,279 alerts that nobody ever read.

Everything here is a plain form POST + redirect. No JSON API for the UI, no
framework, no build step; ``static/app.js`` only adds selection, sorting links
and the shared drawer.
"""

from __future__ import annotations

import os
import secrets

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import config as app_config
from .. import db, queries
from ..meta_links import (
    is_strict_page_id,
    meta_ads_library_url,
    normalize_name,
    page_id_from_url,
)
from ..time_utils import utc_now
from .queue import create_scan_job

bp = Blueprint("pages", __name__)

WORKER_TOKEN_KEY = "worker_token"


@bp.record_once
def _ensure_session_key(state) -> None:
    """Flash messages need a signed session cookie.

    Phase 0's config has no SECRET_KEY (there is no login, so nothing else
    wants one). A per-process random key is exactly right here: the only thing
    in the session is a flash message, the app binds 127.0.0.1, and the
    launcher runs a single worker. Set ``ADSPY2_SECRET_KEY`` if you want
    flashes to survive a restart.
    """
    app = state.app
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = (
            os.environ.get("ADSPY2_SECRET_KEY") or secrets.token_hex(32)
        )

ADD_PAGE_HELP = (
    "Paste the page's Ad Library URL, e.g. "
    "https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
    "&country=ALL&view_all_page_id=123456789012345"
)


# ---------------------------------------------------------------------------
# worker token (shown on the pages screen so the extension can be connected)
# ---------------------------------------------------------------------------
def ensure_worker_token(create: bool = False) -> str:
    """Read the worker token from ``settings``; mint one when asked to.

    Same ``settings`` key that ``/api/worker/hello`` reads, and when
    ``app.job_service`` is present we let *it* mint the token so the extension
    and this screen can never disagree about what the token is. Reading never
    creates: the screen shows a "Generate" button instead, so simply opening
    the dashboard does not quietly hand out a credential.
    """
    token = queries.get_setting(WORKER_TOKEN_KEY)
    if token or not create:
        return token

    try:
        from .. import job_service
    except ImportError:  # pragma: no cover - only before Phase 2 lands
        job_service = None
    if job_service is not None and hasattr(job_service, "worker_token"):
        return str(job_service.worker_token() or "")

    token = secrets.token_urlsafe(32)
    now = utc_now()
    with db.transaction():
        db.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (WORKER_TOKEN_KEY, token, now),
        )
    return token


# ---------------------------------------------------------------------------
# pages list
# ---------------------------------------------------------------------------
@bp.get("/")
@bp.get("/pages")
def index():
    search = request.args.get("q", "").strip()
    sort = request.args.get("sort")
    direction = request.args.get("dir")
    include_hidden = request.args.get("hidden") == "1"
    changed_only = request.args.get("changed") == "1"

    _clause, sort_key, sort_dir = queries.page_order_by(sort, direction)
    rows = queries.list_pages(
        search=search,
        sort=sort_key,
        direction=sort_dir,
        include_hidden=include_hidden,
        changed_only=changed_only,
    )

    return render_template(
        "pages.html",
        title="Pages",
        active_nav="pages",
        pages=rows,
        overview=queries.pages_overview(),
        search=search,
        sort=sort_key,
        direction=sort_dir,
        include_hidden=include_hidden,
        changed_only=changed_only,
        worker_token=ensure_worker_token(),
        dashboard_url=app_config.BASE_URL,
        stale_warn_days=queries.STALE_WARN_DAYS,
        stale_bad_days=queries.STALE_BAD_DAYS,
        add_page_help=ADD_PAGE_HELP,
    )


def _back_to_list() -> str:
    return request.form.get("next") or url_for("pages.index")


# ---------------------------------------------------------------------------
# add a page (P0.1: paste an Ad Library URL)
# ---------------------------------------------------------------------------
def parse_page_input(raw: str) -> tuple[str, str]:
    """(platform_page_id, error). Accepts an Ad Library URL or a bare numeric id.

    Strict on purpose (docs/04 R5): the identity of a page is Facebook's own
    numeric page id from ``view_all_page_id``. Guessing one from a vanity slug
    would merge ads onto the wrong page, and that is unrecoverable.
    """
    text = str(raw or "").strip()
    if not text:
        return "", "Paste an Ad Library URL (or a numeric page id) first."

    if is_strict_page_id(text):
        return text, ""

    from_url = page_id_from_url(text)
    if from_url and is_strict_page_id(from_url):
        return from_url, ""
    if from_url:
        return "", (
            f"Found id {from_url!r} in that URL, but a Meta page id has at least 5 digits. "
            "Check the link and try again."
        )

    lowered = text.lower()
    if "facebook.com" in lowered and "/ads/library" not in lowered:
        return "", (
            "That is a Facebook page URL, not an Ad Library URL. Open the page's "
            '"Page transparency" → Ad Library view and copy that address. ' + ADD_PAGE_HELP
        )
    if text.isdigit():
        return "", "A Meta page id has at least 5 digits — that number is too short."
    return "", "No view_all_page_id in that URL. " + ADD_PAGE_HELP


@bp.post("/pages/add")
def add_page():
    platform_page_id, error = parse_page_input(request.form.get("page_url", ""))
    if error:
        flash(error, "error")
        return redirect(_back_to_list())

    existing = queries.get_page_by_platform_id(platform_page_id)
    if existing:
        if existing["is_hidden"]:
            with db.transaction():
                db.execute(
                    "UPDATE pages SET is_hidden = 0, is_tracked = 1, updated_at = ? WHERE id = ?",
                    (utc_now(), existing["id"]),
                )
            flash(f"{existing['display_name']} was hidden — tracking it again.", "success")
        else:
            flash(f"{existing['display_name']} is already tracked.", "warning")
        return redirect(url_for("pages.detail", page_id=existing["id"]))

    name = request.form.get("name", "").strip()
    now = utc_now()
    with db.transaction():
        cursor = db.execute(
            """
            INSERT INTO pages (platform_page_id, name, normalized_name, url,
                               is_tracked, is_hidden, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, 0, ?, ?)
            """,
            (
                platform_page_id,
                name,
                normalize_name(name),
                meta_ads_library_url(platform_page_id),
                now,
                now,
            ),
        )
        page_id = int(cursor.lastrowid)

    label = name or f"page {platform_page_id}"
    if request.form.get("scan_now"):
        result = create_scan_job([page_id], label=label)
        if result["job_id"]:
            flash(f"Added {label} and queued a scan (job #{result['job_id']}).", "success")
            return redirect(url_for("queue.queue_index"))
    flash(f"Added {label}. Hit Re-track to scan it.", "success")
    return redirect(_back_to_list())


# ---------------------------------------------------------------------------
# hide / alias
# ---------------------------------------------------------------------------
@bp.post("/pages/<int:page_id>/hide")
def hide_page(page_id: int):
    page = queries.get_page(page_id)
    if page is None:
        flash("That page does not exist.", "error")
        return redirect(_back_to_list())
    hidden = 0 if page["is_hidden"] else 1
    with db.transaction():
        db.execute(
            "UPDATE pages SET is_hidden = ?, updated_at = ? WHERE id = ?",
            (hidden, utc_now(), page_id),
        )
    flash(
        f"{page['display_name']} {'hidden' if hidden else 'un-hidden'}.",
        "success",
    )
    return redirect(_back_to_list())


@bp.post("/pages/<int:page_id>/alias")
def set_alias(page_id: int):
    page = queries.get_page(page_id)
    if page is None:
        flash("That page does not exist.", "error")
        return redirect(_back_to_list())
    alias = request.form.get("alias", "").strip()
    with db.transaction():
        db.execute(
            "UPDATE pages SET alias = ?, updated_at = ? WHERE id = ?",
            (alias or None, utc_now(), page_id),
        )
    flash("Alias updated." if alias else "Alias cleared.", "success")
    return redirect(request.form.get("next") or url_for("pages.detail", page_id=page_id))


# ---------------------------------------------------------------------------
# re-track
# ---------------------------------------------------------------------------
def _names(pages: list, limit: int = 4) -> str:
    shown = ", ".join(p["display_name"] for p in pages[:limit])
    return shown if len(pages) <= limit else f"{shown} +{len(pages) - limit} more"


def _flash_job_result(result: dict) -> None:
    unscannable = result.get("unscannable") or []
    if result["job_id"]:
        ids = ", ".join(f"#{i}" for i in result.get("job_ids") or [result["job_id"]])
        flash(f"Job(s) {ids} queued: {_names(result['queued'])}.", "success")
    elif result["reason"] == "already_queued":
        flash("Those pages are already queued or running — not queuing them twice.", "warning")
    elif result["reason"] == "no_meta_page_id":
        pass                                  # the specific message below says it better
    else:
        flash("Nothing to queue: select at least one page.", "warning")
    if result["job_id"] and result["skipped"]:
        flash(f"Already in the queue, skipped: {_names(result['skipped'])}.", "warning")
    if unscannable:
        # Almost all of these come from the v1 import, where a page could be
        # identified by name alone. There is no Ad Library URL to open for them,
        # so saying "queued" would be a lie the worker discovers 35 seconds later.
        flash(
            f"Can't scan {_names(unscannable)}: no Meta page id. Open the page in the "
            "Ad Library and add it again from that URL (the one with "
            "view_all_page_id=...) — the ads already collected stay either way.",
            "warning",
        )


@bp.post("/pages/retrack")
def retrack_selected():
    page_ids = request.form.getlist("page_ids")
    result = create_scan_job(page_ids)
    _flash_job_result(result)
    if result["job_id"]:
        return redirect(url_for("queue.queue_index"))
    return redirect(_back_to_list())


@bp.post("/pages/<int:page_id>/retrack")
def retrack_one(page_id: int):
    result = create_scan_job([page_id])
    _flash_job_result(result)
    if result["job_id"]:
        return redirect(url_for("queue.queue_index"))
    return redirect(_back_to_list())


@bp.post("/pages/worker-token")
def issue_worker_token():
    token = ensure_worker_token(create=True)
    flash("Worker token ready — paste it into the extension's settings." if token
          else "Could not create a worker token.", "success" if token else "error")
    return redirect(_back_to_list())


# ---------------------------------------------------------------------------
# page detail
# ---------------------------------------------------------------------------
@bp.get("/pages/<int:page_id>")
def detail(page_id: int):
    page = queries.get_page(page_id)
    if page is None:
        return render_template("base.html", title="Page not found", not_found=True), 404

    status = request.args.get("status", "active")
    if status not in ("active", "inactive", "all"):
        status = "active"
    sort = request.args.get("sort", queries.DEFAULT_AD_SORT)
    if sort not in queries.AD_SORTS:
        sort = queries.DEFAULT_AD_SORT
    search = request.args.get("q", "").strip()

    ads = queries.page_ads(page_id, status=status, search=search, sort=sort, limit=300)
    metrics = queries.page_daily_metrics(page_id, days=14)

    return render_template(
        "page_detail.html",
        title=page["display_name"],
        active_nav="pages",
        page=page,
        ads=ads,
        counts=queries.page_ad_counts(page_id),
        new_ads=queries.page_new_ads(page_id, limit=60),
        stopped_ads=queries.page_stopped_ads(page_id, limit=60),
        window=queries.last_scan_window(page_id),
        metrics=metrics,
        sparkline=queries.sparkline_points([m["active_ads"] for m in metrics]),
        history=queries.page_scan_history(page_id),
        status=status,
        sort=sort,
        search=search,
        ad_sorts=list(queries.AD_SORTS),
    )


# ---------------------------------------------------------------------------
# drawer fragments (rendered by Jinja, injected by static/app.js)
# ---------------------------------------------------------------------------
@bp.get("/ui/drawer/ad/<int:ad_id>")
def drawer_ad(ad_id: int):
    ad = queries.get_ad(ad_id)
    if ad is None:
        return render_template("_drawer.html", drawer_mode="missing", what="ad"), 404
    return render_template(
        "_drawer.html",
        drawer_mode="ad",
        ad=ad,
        versions=queries.ad_versions(ad_id),
        products=queries.ad_products(ad_id),
    )


@bp.get("/ui/drawer/page/<int:page_id>/changes")
def drawer_changes(page_id: int):
    changes = queries.page_changes(page_id, limit=60)
    if changes["page"] is None:
        return render_template("_drawer.html", drawer_mode="missing", what="page"), 404
    return render_template(
        "_drawer.html",
        drawer_mode="changes",
        page=changes["page"],
        window=changes["window"],
        new_ads=changes["new_ads"],
        stopped_ads=changes["stopped_ads"],
    )


__all__ = ["bp", "ensure_worker_token", "parse_page_input"]
