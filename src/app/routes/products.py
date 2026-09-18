"""Products screen — v1's ``/products`` tab, rebuilt as server-rendered Jinja.

v1's version is one 110k-line Python file that prints a `<style>` block and
then drives the whole screen from a 1,600-line inline script; every filter, tab
and row action is a fetch against a JSON API. v2 keeps the *screen* — the same
seven tabs in the same order, the same seven columns, the same sort list, the
same advanced-filter modal, the same four state toggles — and drops the client
runtime: every control is a GET link or a form POST, and the only JavaScript
involved is the shared drawer opener in ``static/app.js``.

Layout parity (v1 tabs/product/ui.py:571-607):

    tabs      Page-Wise | Page sets | Full List | Tracked | Saved | Favorite |
              Hidden                                    + "Extract All" action
    left      advertiser-page picker (search, select visible, clear, save
              pages, save as set)
    tools     search | sort select | Advanced filters
    table     Product | Advertiser | Active | Represented | Ad age | Pages |
              Actions

Two things this module is careful about:

**The row opens a drawer.** Clicking a product must not navigate away from a
filtered, sorted, paginated list the owner spent four clicks building — so the
product-name cell carries ``data-drawer`` and the drawer fragment below is what
loads. ``/products/<id>`` stays a real URL for deep links, bookmarks and
JS-disabled browsers; it renders the identical body inside the shell.

**Transcription never starts by itself.** It costs money per minute of audio, so
``app/transcription.py`` splits queueing (free) from processing (not), and the
only caller of the processing half is ``transcribe()`` below — a POST, from a
button, with a confirm on it. When no API key is configured the button is
disabled and *says why*; it never fails silently and it never half-runs.
"""

from __future__ import annotations

import csv
import io
import os
import secrets

import re

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from .. import db
from .. import product_service as svc
from .. import transcription
from ..meta_links import meta_ads_library_url

bp = Blueprint("products", __name__)

_THUMB_NAME_RE = re.compile(r"^[0-9a-f]{64}\.jpg$")

EXTRACT_DISABLED_NOTE = (
    "Product extraction runs in the extension worker; this button lands with "
    "the extraction phase."
)
# Product extraction is a separate work item (docs/09-work-order.md Group F)
# and belongs to the ad-content regression, not to this screen.
MAX_TRANSCRIBE_ADS = 200


@bp.record_once
def _ensure_session_key(state) -> None:
    """flash() needs a signed session; same guard as app/routes/pages.py."""
    app = state.app
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = (
            os.environ.get("ADSPY2_SECRET_KEY") or secrets.token_hex(32)
        )
    # The ad drawer (app/routes/pages.py) renders _drawer.html without knowing
    # about transcription. Rather than teach that route this module's tables,
    # the template asks for the ad's transcript through this read-only global;
    # _drawer.html guards on `is defined`, so the drawer still renders when
    # this blueprint is absent.
    app.jinja_env.globals.setdefault(
        "ad_transcript_for", transcription.ad_transcript_for
    )


# ---------------------------------------------------------------------------
# shared request parsing
# ---------------------------------------------------------------------------
def _selected_page_ids() -> list[int]:
    return svc.id_list(request.args.getlist("page_ids"))


def _list_context() -> dict:
    """Everything the querystring says about the list, in one dict.

    Kept in one place because every link on the screen has to preserve the
    other controls' state — v1 got this for free from a JS state object.
    """
    section = svc.clean_text(request.args.get("section", svc.DEFAULT_PRODUCT_SECTION), 32)
    if section not in svc.PRODUCT_SECTIONS:
        section = svc.DEFAULT_PRODUCT_SECTION
    sort = svc.clean_text(request.args.get("sort", svc.DEFAULT_PRODUCT_SORT), 32)
    if sort not in svc.PRODUCT_SORTS:
        sort = svc.DEFAULT_PRODUCT_SORT
    return {
        "section": section,
        "sort": sort,
        "search": svc.clean_text(request.args.get("q", "")),
        "page_ids": _selected_page_ids(),
        "filters": svc.normalize_product_filters(request.args),
        "page": svc.positive_int(request.args.get("page"), 1),
        "picker_search": svc.clean_text(request.args.get("pq", "")),
        "picker_page": svc.positive_int(request.args.get("pp"), 1),
        "set_search": svc.clean_text(request.args.get("sq", "")),
        "show_filters": request.args.get("filters") == "1",
    }


def _list_args(context: dict, **overrides) -> dict:
    """The querystring for a link that changes one control and keeps the rest."""
    filters = context["filters"]
    args: dict = {
        "section": context["section"],
        "sort": context["sort"],
        "q": context["search"] or None,
        "page_ids": context["page_ids"] or None,
        "page": context["page"] if context["page"] > 1 else None,
        "pq": context["picker_search"] or None,
        "pp": context["picker_page"] if context["picker_page"] > 1 else None,
        "sq": context["set_search"] or None,
        "filters": "1" if context["show_filters"] else None,
        "ad_status": filters["ad_status"] if filters["ad_status"] != "all" else None,
        "media_type": filters["media_type"] if filters["media_type"] != "all" else None,
        "has_url": filters["has_url"] if filters["has_url"] != "all" else None,
    }
    for key in ("min_active", "max_active", "min_represented", "max_represented",
                "min_age", "max_age", "min_pages", "max_pages"):
        args[key] = filters[key]
    args.update(overrides)
    return {key: value for key, value in args.items() if value not in (None, "", [])}


# ---------------------------------------------------------------------------
# the screen
# ---------------------------------------------------------------------------
@bp.get("/products")
def index():
    context = _list_context()

    result = svc.list_products(
        section=context["section"],
        search=context["search"],
        sort=context["sort"],
        page_ids=context["page_ids"],
        filters=context["filters"],
        page=context["page"],
    )
    picker = svc.advertiser_pages(
        search=context["picker_search"], page=context["picker_page"], per_page=60
    )
    return render_template(
        "products.html",
        title="Products",
        active_nav="product",
        ctx=context,
        result=result,
        summary=svc.product_summary(context["page_ids"]),
        picker=picker,
        selected_pages=svc.pages_by_id(context["page_ids"]),
        page_sets=svc.list_page_sets(context["set_search"]),
        sort_options=svc.PRODUCT_SORT_OPTIONS,
        ad_status_options=svc.AD_STATUS_OPTIONS,
        media_type_options=svc.MEDIA_TYPE_OPTIONS,
        has_url_options=svc.HAS_URL_OPTIONS,
        filter_count=svc.active_filter_count(context["filters"]),
        extract_note=EXTRACT_DISABLED_NOTE,
        list_args=_list_args,
    )


# ---------------------------------------------------------------------------
# one product: the drawer fragment and the same body as a real page
# ---------------------------------------------------------------------------
def _detail_context(product_id: int) -> dict | None:
    product = svc.get_product(product_id)
    if product is None:
        return None

    columns = svc.resolve_columns(request.args.get("cols"))
    language = svc.clean_text(request.args.get("lang", ""), 12).lower()
    if language and language not in {row["code"] for row in svc.product_languages(product_id)}:
        language = ""
    scripts_open = request.args.get("scripts") == "1"
    scripts_view = "flat" if request.args.get("view") == "flat" else "grouped"
    script_lang = svc.clean_text(request.args.get("slang", ""), 12).lower()
    columns_open = request.args.get("columns") == "1"

    ads = svc.product_ads(product_id, language=language)
    # "cards" is the picture view of the same ads, through the one shared ad
    # tile in _drawer.html. The table stays the default because it is what gets
    # sorted and exported.
    view_ads = svc.clean_text(request.args.get("ads", ""), 8).lower()
    ads_view = "cards" if view_ads == "cards" else "table"
    video_lang = svc.clean_text(request.args.get("vlang", ""), 12).lower()
    connection = db.get_db()
    return {
        "product": product,
        "ads": ads,
        "ads_view": ads_view,
        "ad_cards": (
            svc.product_ad_cards(product_id, language=language)
            if ads_view == "cards" else []
        ),
        # --- the grouping view -------------------------------------------
        "reach": svc.product_reach(product_id),
        "brand_groups": svc.product_groups(product_id),
        "page_rollup": svc.product_page_rollup(product_id),
        # --- transcription (reads only; nothing here starts anything) ------
        **_transcription_context(connection, product_id, video_lang),
        "columns": columns,
        "all_column_labels": svc.AD_COLUMN_LABELS,
        "all_columns": svc.AD_COLUMNS,
        "columns_open": columns_open,
        # The Columns picker's links: each one is "the same URL with this
        # column toggled / moved", computed here so the template stays markup.
        "toggle_cols": svc.toggle_column,
        "move_cols": svc.move_column,
        "languages": svc.product_languages(product_id),
        "language": language,
        "totals": svc.product_intel_totals(product_id),
        "scripts_open": scripts_open,
        "scripts_view": scripts_view,
        "script_lang": script_lang,
        "script_languages": svc.script_language_options(product_id),
        "clusters": (
            svc.product_script_clusters(product_id, language=script_lang)
            if scripts_open and scripts_view == "grouped" else []
        ),
        "transcripts": (
            svc.product_transcripts(product_id, language=script_lang)
            if scripts_open and scripts_view == "flat" else []
        ),
        "cell": svc.ad_column_value,
        "library_url_for": meta_ads_library_url,
        "back": request.args.get("next") or url_for("products.index"),
    }


def _transcription_context(connection, product_id: int, video_lang: str = "") -> dict:
    """Everything the transcription panel + per-video table read. Shared by
    the drawer/detail body and the polled fragment so the two never disagree.
    Reads only — nothing in here queues, refreshes or spends."""
    pre_flight = transcription.media_state(connection, product_id)
    # What a run would actually SEND — not "video ads". Already-done creatives
    # (by hash OR by an existing link), shared creatives and media-less ads all
    # drop out, so the confirm dialog quotes the real bill. media_state's
    # counts override the SQL-only ones; estimate_new is the number the run
    # itself uses, so it is the one the dialog quotes.
    pre_flight["to_send"] = transcription.estimate_new(connection, product_id)
    return {
        "transcription": dict(svc.transcription_state(product_id), **pre_flight),
        "transcribe_languages": svc.product_language_queue(product_id),
        "provider": transcription.provider_status(connection),
        "run": transcription.latest_run(connection, product_id),
        "videos": transcription.product_videos(connection, product_id, language=video_lang),
        "video_summary": transcription.video_language_summary(connection, product_id),
        "video_lang": video_lang,
        "video_languages": svc.script_language_options(product_id),
    }


@bp.get("/ui/drawer/product/<int:product_id>")
def drawer_product(product_id: int):
    context = _detail_context(product_id)
    if context is None:
        return render_template("products.html", drawer=True, missing=True), 404
    return render_template("products.html", drawer=True, **context)


@bp.get("/products/<int:product_id>")
def detail(product_id: int):
    context = _detail_context(product_id)
    if context is None:
        return render_template("base.html", title="Product not found", not_found=True), 404
    return render_template(
        "products.html",
        title=context["product"]["product_name"],
        active_nav="product",
        detail=True,
        **context,
    )


@bp.get("/products/<int:product_id>/linked-ads.csv")
def export_linked_ads(product_id: int):
    """v1's "Export CSV" on the linked-ads table: the visible columns, in the
    visible order, for every linked ad the current language filter shows."""
    product = svc.get_product(product_id)
    if product is None:
        return {"ok": False, "error": "not found", "code": "NOT_FOUND"}, 404

    columns = svc.resolve_columns(request.args.get("cols"))
    language = svc.clean_text(request.args.get("lang", ""), 12).lower()
    ads = svc.product_ads(product_id, language=language, limit=5000)

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(columns)
    for ad in ads:
        writer.writerow([svc.ad_column_value(ad, key) for key in columns])

    slug = "".join(
        char if char.isalnum() else "-" for char in product["product_name"].lower()
    ).strip("-")[:60] or "product"
    # BOM first: Excel opens a UTF-8 CSV as mojibake without it, and these
    # product names are Hindi/Tamil more often than not.
    body = "﻿" + buffer.getvalue()
    return Response(
        body,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{slug}-linked-ads.csv"'},
    )


@bp.get("/products/<int:product_id>/scripts.txt")
def export_scripts(product_id: int):
    """v1's "Download .txt" under the Scripts panel: every transcript this
    product's ads carry, one block each, in the language currently filtered."""
    product = svc.get_product(product_id)
    if product is None:
        return {"ok": False, "error": "not found", "code": "NOT_FOUND"}, 404

    language = svc.clean_text(request.args.get("slang", ""), 12).lower()
    rows = svc.product_transcripts(product_id, language=language)
    blocks = [
        "\n".join([
            f"[{row.get('library_id') or '-'}] ({row['language_name']})"
            + (f"  {row['script_label']}" if row.get("script_label") else ""),
            f"Hook: {row.get('hook') or '-'}",
            str(row.get("transcript") or ""),
        ])
        for row in rows
    ]
    body = f"{product['product_name']}\n\n" + "\n\n---\n\n".join(blocks)
    slug = "".join(
        char if char.isalnum() else "-" for char in product["product_name"].lower()
    ).strip("-")[:60] or "product"
    return Response(
        body,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{slug}-scripts.txt"'},
    )


# ---------------------------------------------------------------------------
# transcription — the only thing on this screen that spends money
# ---------------------------------------------------------------------------
@bp.get("/ui/products/<int:product_id>/transcription")
def transcription_fragment(product_id: int):
    """The progress strip, polled by static/app.js while a run is live.

    A fragment rather than JSON for the same reason as every other fragment in
    this app: the markup is Jinja's, so no screen builds ad or run markup in
    JavaScript.
    """
    connection = db.get_db()
    video_lang = svc.clean_text(request.args.get("vlang", ""), 12).lower()
    return render_template(
        "products.html",
        transcription_panel=True,
        product=svc.get_product(product_id),
        **_transcription_context(connection, product_id, video_lang),
        reach=svc.product_reach(product_id),
    )


@bp.get("/media/thumbs/<name>")
def media_thumb(name: str):
    """A locally extracted poster frame, `<content sha256>.jpg`. The name is
    checked against the hash shape before it goes anywhere near a path, and
    send_from_directory refuses anything that escapes the folder anyway."""
    if not _THUMB_NAME_RE.fullmatch(str(name or "")):
        abort(404)
    folder = transcription.thumbs_dir(db.get_db())
    if not (folder / name).is_file():
        abort(404)
    return send_from_directory(str(folder), name, mimetype="image/jpeg", max_age=86400)


@bp.post("/products/<int:product_id>/transcribe")
def transcribe(product_id: int):
    """Generate scripts for this product's video ads. Owner-initiated, always.

    There is no other caller: nothing in ingest, nothing in the worker API and
    nothing on a timer reaches ``transcription.start_run``. That is the point,
    and ``tests/test_product_experience.py`` asserts it by grepping the tree.
    """
    product = svc.get_product(product_id)
    if product is None:
        flash("That product no longer exists.", "error")
        return redirect(_back())

    languages = [
        svc.clean_text(code, 12).lower()
        for code in request.form.getlist("languages")
        if svc.clean_text(code, 12)
    ]
    result = transcription.start_run(
        product_id, languages=languages or None, conn=db.get_db()
    )
    if result.get("ok"):
        run = result.get("run") or {}
        flash(
            f"Transcribing {run.get('total', 0)} creative(s) for "
            f"{product['product_name']}. This costs API credit and runs in the "
            "background; the panel updates as it goes.",
            "success",
        )
    elif result.get("reason") == "no_api_key":
        flash(result.get("message") or "No transcription API key is configured.",
              "warning")
    elif result.get("reason") == "already_running":
        run = result.get("run") or {}
        flash(
            f"Run #{run.get('id')} is still going for {product['product_name']} — "
            "one run per product at a time, so nothing is billed twice. Cancel it "
            "first if it is stuck.",
            "warning",
        )
    else:
        queued = result.get("queued") or {}
        if queued.get("no_media") or queued.get("not_video"):
            missing = int(queued.get("no_media") or 0)
            pictures = int(queued.get("not_video") or 0)
            parts = []
            if missing:
                parts.append(f"{missing} carry no stored media URL")
            if pictures:
                parts.append(f"{pictures} carry only a picture, not a video")
            flash(
                "Nothing to transcribe: of this product's candidate ads, "
                + " and ".join(parts)
                + ", so there is no audio to send. Re-scan those pages to capture media.",
                "warning",
            )
        elif queued.get("already_done"):
            flash("Every creative for this product is already transcribed.",
                  "success")
        else:
            flash("Nothing to transcribe for this product.", "warning")
    return redirect(_back())


@bp.post("/products/<int:product_id>/transcribe/cancel")
def cancel_transcription(product_id: int):
    """Stop the live run after the creative in flight. Pending rows stay
    pending; the next Generate press resumes where it stopped."""
    run_id = transcription.request_cancel(db.get_db(), product_id)
    flash(
        f"Cancel requested for run #{run_id}; it stops after the current creative."
        if run_id else "No live run to cancel for this product.",
        "success" if run_id else "warning",
    )
    return redirect(_back())


@bp.post("/products/<int:product_id>/transcripts/retry")
def retry_transcripts(product_id: int):
    """Put failed transcripts back in the queue. Queues only — never runs."""
    connection = db.get_db()
    recovered = transcription.recover_stale(connection, product_id)
    count = transcription.retry_failed(connection, product_id)
    total = count + int(recovered.get("recovered") or 0)
    flash(
        f"{total} transcript(s) queued again. Press Generate scripts to run them."
        if total else "No failed transcripts for this product.",
        "success" if total else "warning",
    )
    return redirect(_back())


@bp.post("/products/<int:product_id>/transcripts/<int:transcript_id>/requeue")
def requeue_transcript(product_id: int, transcript_id: int):
    """"Retranscribe this one" on the per-video table. Queues only — the next
    Generate press re-hears it; nothing is billed here."""
    ok = transcription.requeue_transcript(db.get_db(), product_id, transcript_id)
    flash(
        "Creative queued for a fresh transcription. Press Generate scripts to run it."
        if ok else "That creative does not belong to this product.",
        "success" if ok else "error",
    )
    return redirect(_back())


@bp.post("/products/<int:product_id>/languages/detect")
def detect_languages(product_id: int):
    """Free, offline: read each ad's own copy and record its script language,
    so the language checkboxes stop saying Unknown before any money is spent.
    A POST on purpose — read paths never write."""
    count = transcription.backfill_text_languages(
        db.get_db(), product_id=product_id
    )
    flash(
        f"Detected the text language of {count} ad(s) from their own copy."
        if count else "Every ad of this product already has a language recorded.",
        "success" if count else "warning",
    )
    return redirect(_back())


@bp.post("/products/<int:product_id>/media/refresh")
def refresh_media(product_id: int):
    """Re-sign this product's expired video links from the public Ad Library
    page (headless browser, no login, no provider call). Free. Runs in the
    background; the panel polls its progress like a run's."""
    product = svc.get_product(product_id)
    if product is None:
        flash("That product no longer exists.", "error")
        return redirect(_back())
    result = transcription.start_media_refresh(product_id, conn=db.get_db())
    if result.get("ok"):
        flash(
            f"Refreshing expired video links for {product['product_name']} in the "
            "background — one Ad Library page every few seconds, no login.",
            "success",
        )
    elif result.get("reason") == "already_running":
        flash("A link refresh is already running for this product.", "warning")
    else:
        flash(result.get("message") or "Link refresh is not available here.", "warning")
    return redirect(_back())


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------
def _back() -> str:
    """Where a POST returns to. The selection lives in the querystring, so the
    forms that carry a checkbox set put ``next`` there rather than in a hidden
    field (a hidden field would leak into the GET filter form as well)."""
    return (
        request.form.get("next")
        or request.args.get("next")
        or url_for("products.index")
    )


@bp.post("/products/<int:product_id>/state")
def set_state(product_id: int):
    field = svc.clean_text(request.form.get("field", ""), 16)
    if field not in svc.PRODUCT_STATE_FIELDS:
        flash("Unknown product flag.", "error")
        return redirect(_back())
    on = request.form.get("on") == "1"
    if not svc.set_product_state(product_id, field, on):
        flash("Could not update that product.", "error")
    else:
        flash(f"{field.capitalize()} {'on' if on else 'off'}.", "success")
    return redirect(_back())


@bp.post("/products/<int:product_id>/remove")
def remove(product_id: int):
    name = svc.remove_product(product_id)
    flash(
        f"Removed {name} permanently." if name else "That product no longer exists.",
        "success" if name else "warning",
    )
    return redirect(_back())


@bp.post("/products/<int:product_id>/retrack")
def retrack(product_id: int):
    """Re-scan every advertiser page that runs this product."""
    product = svc.get_product(product_id)
    if product is None:
        flash("That product no longer exists.", "error")
        return redirect(_back())
    page_ids = [int(page["page_id"]) for page in product["pages"]]
    if not page_ids:
        flash("No advertiser pages to re-scan for this product.", "warning")
        return redirect(_back())

    from .queue import create_scan_job

    result = create_scan_job(page_ids, label=product["product_name"])
    if result["job_id"]:
        flash(
            f"Job #{result['job_id']} queued: {len(result['queued'])} page(s) "
            f"running {product['product_name']}.",
            "success",
        )
        return redirect(url_for("queue.queue_index"))
    if result["reason"] == "already_queued":
        flash("Those pages are already queued or running.", "warning")
    else:
        flash("Could not queue a scan for those pages.", "warning")
    return redirect(_back())


@bp.post("/products/pages/save")
def save_pages():
    page_ids = svc.id_list(request.form.getlist("page_ids"))
    saved = request.form.get("saved", "1") == "1"
    count = svc.set_page_saved(page_ids, saved)
    flash(
        f"{count} page(s) {'saved' if saved else 'un-saved'}." if count
        else "Select at least one page first.",
        "success" if count else "warning",
    )
    return redirect(_back())


@bp.post("/products/page-sets")
def create_page_set():
    page_ids = svc.id_list(request.form.getlist("page_ids"))
    name = svc.clean_text(request.form.get("name", ""), 80)
    if not page_ids:
        flash("Pick the pages first, then save them as a set.", "warning")
        return redirect(_back())
    if not name:
        flash("Give the set a name.", "warning")
        return redirect(_back())
    svc.create_page_set(name, page_ids)
    flash(f"Saved page set '{name}' with {len(page_ids)} page(s).", "success")
    return redirect(_back())


@bp.post("/products/page-sets/<int:set_id>/delete")
def delete_page_set(set_id: int):
    svc.delete_page_set(set_id)
    flash("Page set deleted.", "success")
    return redirect(_back())


__all__ = ["bp"]
