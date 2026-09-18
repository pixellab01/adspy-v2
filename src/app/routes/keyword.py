"""Keyword Research screen — v1's four sections at v1's URL, plus the
two-stage flow.

    Research   the add-search form + the research queue (stage 1)
    List       every saved search
    Favorites  the starred ones, with a recurring monitor per row
    Details    one search: its metrics, its DISCOVERED PAGES review list
               (stage 2 lives here), its ranked results, its run history

All four are server-rendered sections of one page (``?section=``), so a
bookmark and the browser Back button both work, and every mutation is a plain
form POST + redirect + flash — the same shape as ``app/routes/pages.py``. No
JSON API for the UI.

THE CHAIN HOOK. Bulk mode ("discover, then scan every page found") has to
advance while nobody is looking at the screen — the extension runs overnight.
``keyword_service.advance_chains()`` is idempotent and cheap, and this
blueprint runs it app-wide before every ``POST /api/worker/claim`` and after
every ``POST /api/jobs/<id>/done``. Those two are exactly the moments a
stage-1 job can have just finished, so stage 2 is queued before the extension
asks for its next job. ``app/jobs.py`` and ``app/job_service.py`` stay ignorant
of the keyword tables; the hook lives here, in the module that owns them, and
it can never fail a worker call.

The domain panel: v1 flips the Research tab into "website research" when the
search term looks like a URL. Same here, except the internal-matches half is a
real query against ``products.domain`` instead of a fetch, and v1's warning
survives verbatim — an Ad Library keyword run is a *text* search, so the
fresh-from-Facebook half can never be an exact link match.

WHAT IS STILL MISSING. ``keyword_queries`` in 002 has no
``monitor_frequency`` / ``monitor_next_run_at``, so the Favorites monitor
selector stores its value in the ``settings`` table under
``keyword.monitor.<query_id>`` and there is no scheduler behind it.
"""

from __future__ import annotations

import logging
import os
import re
import secrets

from flask import (
    Blueprint,
    Response,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import keyword_service as ks

log = logging.getLogger("adspy2.keyword")

bp = Blueprint("keyword", __name__)

SECTIONS = ("research", "list", "favorites", "details")

_CLAIM_PATH = "/api/worker/claim"
_DONE_PATH_RE = re.compile(r"^/api/jobs/\d+/done$")


@bp.record_once
def _ensure_session_key(state) -> None:
    app = state.app
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = (
            os.environ.get("ADSPY2_SECRET_KEY") or secrets.token_hex(32)
        )


def _advance(where: str) -> None:
    """Run the chain step; never let it break the request it rides on."""
    try:
        ks.advance_chains()
    except Exception as exc:  # pragma: no cover - a rollup must never 500 a worker call
        log.warning("keyword chain advance failed (%s): %r", where, exc)


@bp.before_app_request
def _chain_before_claim():
    """Stage 2 is queued BEFORE the extension is handed its next job, so a
    completed discover run is followed by its scan without a screen read."""
    if request.method == "POST" and request.path == _CLAIM_PATH:
        _advance("claim")
    return None


@bp.after_app_request
def _chain_after_done(response):
    """And immediately after a job reports done, so the Details screen shows
    the stage-2 job the moment the owner looks."""
    if (
        request.method == "POST"
        and _DONE_PATH_RE.match(request.path or "")
        and response.status_code < 400
    ):
        _advance("done")
    return response


def _section(value: str | None) -> str:
    text = str(value or "").strip().lower()
    return text if text in SECTIONS else "research"


def _back(section: str = "research", **params) -> str:
    """Redirect target that keeps the reader on the section they acted from."""
    return request.form.get("next") or url_for("keyword.index", section=section, **params)


def _query_back(query_id: int | None) -> str:
    return _back("details", query_id=query_id) if query_id else _back("list")


def _flash_scan_result(result: dict, *, what: str = "page") -> None:
    """One honest sentence per outcome of a stage-2 attempt."""
    queued = len(result.get("queued") or [])
    skipped = len(result.get("skipped") or [])
    unscannable = len(result.get("unscannable") or [])
    blocked = len(result.get("blocked") or [])
    if result.get("job_id"):
        flash(
            f"Scan job #{result['job_id']} queued for {queued} {what}{'s' if queued != 1 else ''} "
            "- the extension picks it up on its next claim.",
            "success",
        )
    elif result.get("reason") == "already_queued":
        flash("Those pages are already queued or running - wait for the current job.", "warning")
    elif result.get("reason") == "no_meta_page_id":
        flash("None of those pages has a numeric Meta page id, so there is nothing to open.", "error")
    elif result.get("reason") == "all_blocked":
        flash("Every selected page is blocked - unblock one first.", "warning")
    else:
        flash("Nothing to queue.", "warning")
    if skipped and result.get("job_id"):
        flash(f"{skipped} page(s) skipped - already queued by another job.", "warning")
    if unscannable:
        flash(f"{unscannable} page(s) have no numeric Meta page id and were filed as unscannable.", "warning")
    if blocked:
        flash(f"{blocked} blocked page(s) left out.", "warning")


# ---------------------------------------------------------------------------
# screen
# ---------------------------------------------------------------------------
@bp.get("/keyword-research")
def index():
    # Runs are reconciled against their jobs on read, results materialised and
    # auto-scans fired — ingest and job_service are shared write paths and
    # must not learn about keyword tables.
    _advance("screen")

    section = _section(request.args.get("section"))
    query_id = request.args.get("query_id", type=int)
    run_id = request.args.get("run", type=int)
    view = "run" if request.args.get("view") == "run" else "cumulative"
    review_status = request.args.get("status", "").strip().lower() or None
    term = request.args.get("q", "").strip()

    detail = (
        ks.query_detail(query_id, run_id=run_id, view=view, review_status=review_status)
        if query_id else None
    )
    if query_id and detail is None:
        flash(f"Saved search #{query_id} does not exist.", "error")
    if query_id and detail is not None:
        section = "details"

    domain = ks.looks_like_domain(term)
    return render_template(
        "keyword.html",
        title="Keyword Research",
        active_nav="keyword",
        section=section,
        term=term,
        domain=domain,
        domain_matches=ks.domain_matches(domain) if domain else [],
        queue=ks.queue_items(),
        library=ks.list_queries(page=request.args.get("page", 1, type=int)),
        favorites=ks.list_queries(saved_only=True, page=request.args.get("fav_page", 1, type=int)),
        detail=detail,
        countries=ks.COUNTRIES,
        ad_statuses=ks.AD_STATUSES,
        platforms=ks.PLATFORMS,
        media_types=ks.MEDIA_TYPES,
        monitor_choices=ks.MONITOR_CHOICES,
        review_statuses=ks.REVIEW_STATUSES,
        depth_default=ks.DEPTH_DEFAULT,
        max_pages_default=ks.MAX_PAGES_DEFAULT,
        auto_scan_min_ads_default=ks.auto_scan_min_ads_default(),
    )


# ---------------------------------------------------------------------------
# the research queue (stage 1)
# ---------------------------------------------------------------------------
@bp.post("/keyword-research/queue")
def add_to_queue():
    """v1's "Add to queue", both input modes: one keyword, or one per line.

    Two additions make bulk mode one gesture: ``auto_scan=1`` saves the
    search with automation on (stage 2 fires by itself when stage 1
    completes), and ``start_now=1`` dispatches every run immediately instead
    of leaving it in the queue for a second click.
    """
    filters = ks.filters_from_form(request.form)
    automation = ks.automation_from_form(request.form)
    favorite = request.form.get("favorite") == "1"
    start_now = request.form.get("start_now") == "1"
    mode = request.form.get("mode", "single")

    keywords = (
        ks.split_bulk(request.form.get("bulk_keywords"))
        if mode == "bulk"
        else [k for k in [ks.parse_keyword(request.form.get("keyword"))] if k]
    )
    if not keywords:
        flash("Enter a keyword, or paste an Ad Library search link.", "error")
        return redirect(_back())

    added = 0
    started = 0
    for keyword in keywords:
        try:
            created = ks.enqueue(keyword, favorite=favorite, **filters, **automation)
            added += 1
            if start_now:
                ks.start_run(created["run_id"])
                started += 1
        except ks.KeywordError as exc:
            flash(str(exc), "error")
    if added:
        noun = f"{keywords[0]}" if added == 1 else f"{added} searches"
        verb = "added to the research queue"
        if start_now:
            verb = f"dispatched - {started} job(s) queued for the extension"
        tail = " Auto-scan is on: every page found gets scanned when discovery finishes." if automation["auto_scan"] else ""
        flash(f"{noun} {verb}.{tail}", "success")
    return redirect(_back())


@bp.post("/keyword-research/queue/<int:run_id>/research")
def research(run_id: int):
    try:
        job_id = ks.start_run(run_id)
    except ks.KeywordError as exc:
        flash(str(exc), "error")
        return redirect(_back())
    flash(f"Queued as job #{job_id} - the extension picks it up on its next claim.", "success")
    return redirect(_back())


@bp.post("/keyword-research/queue/start-all")
def research_all():
    """Dispatch every waiting queue item in one click. The extension runs the
    jobs one after another under its own pacing; with auto-scan on, each
    finished discover run queues its own scan."""
    started = 0
    for item in ks.queue_items():
        if item.get("job_id"):
            continue
        try:
            ks.start_run(int(item["id"]))
            started += 1
        except ks.KeywordError as exc:
            flash(str(exc), "error")
    flash(
        f"{started} search(es) dispatched." if started else "Nothing waiting to dispatch.",
        "success" if started else "warning",
    )
    return redirect(_back())


@bp.post("/keyword-research/queue/<int:run_id>/remove")
def remove(run_id: int):
    try:
        ks.remove_queue_item(run_id)
        flash("Removed from the research queue.", "success")
    except ks.KeywordError as exc:
        flash(str(exc), "error")
    return redirect(_back())


# ---------------------------------------------------------------------------
# saved searches
# ---------------------------------------------------------------------------
@bp.post("/keyword-research/queries/<int:query_id>/favorite")
def favorite(query_id: int):
    on = request.form.get("on") == "1"
    ks.set_favorite(query_id, on)
    flash("Added to favorites." if on else "Removed from favorites.", "success")
    return redirect(_back(_section(request.form.get("section")), query_id=request.form.get("query_id", type=int)))


@bp.post("/keyword-research/queries/<int:query_id>/monitor")
def monitor(query_id: int):
    frequency = ks.set_monitor(query_id, request.form.get("frequency", "off"))
    flash(
        "Monitor turned off." if frequency == "off"
        else f"Monitor set to {frequency} - there is no scheduler yet, so run it from the queue.",
        "success" if frequency == "off" else "warning",
    )
    return redirect(_back("favorites"))


@bp.post("/keyword-research/queries/<int:query_id>/research")
def research_query(query_id: int):
    """Detail screen's "Run discovery again": re-run this saved search as it stands."""
    query = ks.get_query(query_id)
    if query is None:
        flash(f"Saved search #{query_id} does not exist.", "error")
        return redirect(_back("list"))
    try:
        created = ks.research_now(
            query["keyword"],
            country=query["country"], ad_status=query["ad_status"],
            platform=query["platform"], media_type=query["media_type"],
            default_depth=query["default_depth"], max_pages=query["max_pages"],
        )
    except ks.KeywordError as exc:
        flash(str(exc), "error")
        return redirect(_back("details", query_id=query_id))
    flash(f"{query['keyword']} queued as job #{created['job_id']}.", "success")
    return redirect(_back("details", query_id=query_id))


@bp.post("/keyword-research/queries/<int:query_id>/auto-scan")
def auto_scan(query_id: int):
    """Bulk mode toggle + its threshold. Applies to runs that finish from now."""
    if ks.get_query(query_id) is None:
        flash(f"Saved search #{query_id} does not exist.", "error")
        return redirect(_back("list"))
    on = request.form.get("on") == "1"
    min_ads = request.form.get("min_ads", type=int)
    state = ks.set_auto_scan(query_id, on, min_ads)
    if request.form.get("save_default") == "1" and min_ads:
        ks.set_auto_scan_min_ads_default(min_ads)
    flash(
        f"Auto-scan on: when a discovery run finishes, every page with {state['min_ads']}+ matching ads is queued for a full scan."
        if state["auto_scan"] else "Auto-scan off - pages wait in the review list until you accept them.",
        "success",
    )
    return redirect(_back("details", query_id=query_id))


# ---------------------------------------------------------------------------
# stage 2: review list actions
# ---------------------------------------------------------------------------
@bp.post("/keyword-research/queries/<int:query_id>/pages/<int:page_id>/review")
def review_page(query_id: int, page_id: int):
    action = request.form.get("action", "")
    try:
        result = ks.review_page(query_id, page_id, action)
    except ks.KeywordError as exc:
        flash(str(exc), "error")
        return redirect(_query_back(query_id))
    if action == "scan":
        _flash_scan_result(result)
    elif action == "accept":
        flash("Accepted and tracked - press Scan now or Scan all to scrape its ads.", "success")
    elif action == "ignore":
        flash("Ignored - it stays out of Scan all.", "success")
    else:
        flash("Back to New.", "success")
    return redirect(_query_back(query_id))


@bp.post("/keyword-research/queries/<int:query_id>/scan-selected")
def scan_selected(query_id: int):
    """"Scan selected (N)" on the review list."""
    page_ids = [int(v) for v in request.form.getlist("page_id") if str(v).isdigit()]
    if not page_ids:
        flash("Select at least one page first.", "error")
        return redirect(_query_back(query_id))
    query = ks.get_query(query_id)
    if query is None:
        flash(f"Saved search #{query_id} does not exist.", "error")
        return redirect(_back("list"))
    result = ks.stage2_scan(query_id, page_ids, label=f"Keyword scan: {query['keyword']}")
    _flash_scan_result(result)
    return redirect(_query_back(query_id))


@bp.post("/keyword-research/queries/<int:query_id>/scan-all")
def scan_all(query_id: int):
    """"Scan all new + accepted": stage 2 for every scannable discovered page."""
    if ks.get_query(query_id) is None:
        flash(f"Saved search #{query_id} does not exist.", "error")
        return redirect(_back("list"))
    result = ks.scan_all(query_id)
    if result.get("reason") == "no_pages" and not result.get("job_id"):
        flash("No new or accepted pages with a numeric Meta page id to scan.", "warning")
    else:
        _flash_scan_result(result)
    return redirect(_query_back(query_id))


@bp.post("/keyword-research/analyze")
def analyze_selected():
    """v1's "Scrape selected pages (N)" on the ranked table and the domain
    panel. With a ``query_id`` it is stage 2 (review rows get updated); without
    one it is a plain scan job through the same guarded path."""
    page_ids = [int(v) for v in request.form.getlist("page_id") if str(v).isdigit()]
    query_id = request.form.get("query_id", type=int)
    if not page_ids:
        flash("Select at least one page first.", "error")
        return redirect(_query_back(query_id) if query_id else _back())

    label = request.form.get("label") or f"Keyword pages ({len(page_ids)})"
    if query_id:
        result = ks.stage2_scan(query_id, page_ids, label=label)
    else:
        from .queue import create_scan_job

        result = create_scan_job(page_ids, label=label)
    _flash_scan_result(result)
    return redirect(_query_back(query_id) if query_id else _back())


# ---------------------------------------------------------------------------
# results: block / unblock
# ---------------------------------------------------------------------------
@bp.post("/keyword-research/pages/block")
def block():
    page_id = request.form.get("page_id", type=int)
    name = request.form.get("page_name", "")
    try:
        ks.block_page(page_id, request.form.get("reason", "Blocked from Keyword Research"))
        flash(f"{name or 'Page'} blocked - it is hidden from results and skipped in future scrapes.", "success")
    except ks.KeywordError as exc:
        flash(str(exc), "error")
    return redirect(_back("details", query_id=request.form.get("query_id", type=int)))


@bp.post("/keyword-research/blocks/<int:block_id>/unblock")
def unblock(block_id: int):
    ks.unblock_page(block_id)
    flash("Page unblocked.", "success")
    return redirect(_back("details", query_id=request.form.get("query_id", type=int)))


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
@bp.get("/keyword-research/runs/<int:run_id>/export.csv")
def export_csv(run_id: int):
    try:
        filename, body = ks.export_run_csv(run_id)
    except ks.KeywordError as exc:
        flash(str(exc), "error")
        return redirect(url_for("keyword.index"))
    return Response(
        body,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.get("/keyword-research/queries/<int:query_id>/discovered.csv")
def export_discovered_csv(query_id: int):
    try:
        filename, body = ks.export_discovered_csv(query_id)
    except ks.KeywordError as exc:
        flash(str(exc), "error")
        return redirect(url_for("keyword.index"))
    return Response(
        body,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


__all__ = ["bp"]
