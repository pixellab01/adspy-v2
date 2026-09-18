"""Brand Groups — v1's ``/brand-groups`` tab.

v1's shell is a 238px group rail on the left and a detail pane on the right
(tabs/brand_group/ui.py:370-381); v2 keeps that shape using app.css's
``.groups-layout`` / ``.group-rail`` (which were written for exactly this), and
keeps the detail pane's furniture item for item:

    head   <name> / <primary domain>   [Reload] [Copy pages] [Add pages]
                                       [Edit] [Delete]
    KPIs   Pages · Active ads · Represented · Products · Video ads · Oldest days
    tabs   Pages | Products
    pages  Page | Sources | FB results | Ads scraped | Boxes | Products |
           Oldest | Top product | Actions
    prods  Product | Domain | Active | Represented | Pages | Age | Video |
           Image | Actions

Two screens, two templates: ``group_list.html`` is the rail with the "Select a
brand group" pane, ``group_detail.html`` is the rail plus the detail. Both are
plain GETs, so a group is a real URL you can bookmark — v1 kept the selection
in JS state and a hash.
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

from .. import product_service as svc

bp = Blueprint("groups", __name__)


@bp.record_once
def _ensure_session_key(state) -> None:
    app = state.app
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = (
            os.environ.get("ADSPY2_SECRET_KEY") or secrets.token_hex(32)
        )


# ---------------------------------------------------------------------------
# shared
# ---------------------------------------------------------------------------
def _rail_context() -> dict:
    sort = svc.clean_text(request.args.get("gsort", svc.DEFAULT_GROUP_SORT), 24)
    if sort not in svc.GROUP_SORTS:
        sort = svc.DEFAULT_GROUP_SORT
    search = svc.clean_text(request.args.get("gq", ""))
    groups = svc.list_groups(sort=sort, search=search)
    return {
        "groups": groups,
        "group_sort": sort,
        "group_search": search,
        "group_sort_options": svc.GROUP_SORT_OPTIONS,
        "rail_total": sum(int(row["page_count"] or 0) for row in groups),
    }


def _picker_context(group_id: int | None) -> dict:
    scope = svc.clean_text(request.args.get("scope", "ungrouped"), 16)
    if scope not in dict(svc.GROUP_PAGE_SCOPES):
        scope = "ungrouped"
    sort = svc.clean_text(request.args.get("psort", "active_desc"), 24)
    if sort not in dict(svc.GROUP_PICKER_SORTS):
        sort = "active_desc"
    search = svc.clean_text(request.args.get("psearch", ""))
    return {
        "picker_scope": scope,
        "picker_sort": sort,
        "picker_search": search,
        "picker_scopes": svc.GROUP_PAGE_SCOPES,
        "picker_sorts": svc.GROUP_PICKER_SORTS,
        "picker_pages": svc.group_picker_pages(
            group_id=group_id, scope=scope, search=search, sort=sort
        ),
    }


# ---------------------------------------------------------------------------
# screens
# ---------------------------------------------------------------------------
@bp.get("/brand-groups")
def index():
    rail = _rail_context()
    new_open = request.args.get("new") == "1"

    # v1 does not land this screen on a prompt: it renders the first group of
    # the rail straight into the detail pane, so opening Brand Groups shows
    # Naaptol's pages immediately. v2 showed "Select a brand group" instead —
    # an empty pane exactly where v1 has content, which is the owner's "same
    # screens, same places" complaint. So the default lands on a group too.
    #
    # Three things still reach the list itself: ?new=1 (the create dialog must
    # not be bounced past), ?all=1 (v2's own "All brand groups" summary table,
    # which v1 has no counterpart for), and having no groups at all — with an
    # empty rail there is nothing to select and the prompt is the right screen.
    if rail["groups"] and not new_open and request.args.get("all") != "1":
        carry = {k: v for k, v in request.args.items() if k in ("gq", "gsort") and v}
        return redirect(
            url_for("groups.detail", group_id=int(rail["groups"][0]["id"]), **carry)
        )

    return render_template(
        "group_list.html",
        title="Brand Groups",
        active_nav="brand_group",
        new_open=new_open,
        **rail,
        **(_picker_context(None) if new_open else {}),
    )


@bp.get("/brand-groups/<int:group_id>")
def detail(group_id: int):
    group = svc.get_group(group_id)
    if group is None:
        return render_template("base.html", title="Group not found", not_found=True), 404

    rail = _rail_context()
    tab = "products" if request.args.get("tab") == "products" else "pages"

    pages = svc.group_pages(
        group_id=group_id,
        category=svc.clean_text(request.args.get("cat", "all"), 24),
        order=svc.clean_text(request.args.get("ord", "live_desc"), 24),
        search=svc.clean_text(request.args.get("q", "")) if tab == "pages" else "",
        page=svc.positive_int(request.args.get("page"), 1) if tab == "pages" else 1,
    )
    products = svc.group_products(
        group_id=group_id,
        visibility=svc.clean_text(request.args.get("vis", "visible"), 16),
        sort=svc.clean_text(request.args.get("psort", "active_desc"), 24),
        age_bucket=svc.clean_text(request.args.get("age", "all"), 24),
        min_active=svc.optional_int(request.args.get("min_active")),
        min_represented=svc.optional_int(request.args.get("min_represented")),
        min_pages=svc.optional_int(request.args.get("min_pages")),
        search=svc.clean_text(request.args.get("q", "")) if tab == "products" else "",
        page=svc.positive_int(request.args.get("page"), 1) if tab == "products" else 1,
    )

    edit_open = request.args.get("edit") == "1"
    add_open = request.args.get("add") == "1"
    return render_template(
        "group_detail.html",
        title=group["name"],
        active_nav="brand_group",
        group=group,
        summary=svc.group_summary(group_id),
        tab=tab,
        pages=pages,
        products=products,
        page_names=svc.group_page_names(group_id),
        page_categories=svc.GROUP_PAGE_CATEGORIES,
        page_orders=svc.GROUP_PAGE_ORDERS,
        product_visibility=svc.GROUP_PRODUCT_VISIBILITY,
        product_sorts=svc.GROUP_PRODUCT_SORTS,
        product_ages=svc.GROUP_PRODUCT_AGES,
        edit_open=edit_open,
        add_open=add_open,
        **rail,
        **(_picker_context(group_id) if (add_open or edit_open) else {}),
    )


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------
def _back(group_id: int | None = None) -> str:
    if request.form.get("next"):
        return request.form["next"]
    if group_id:
        return url_for("groups.detail", group_id=group_id)
    return url_for("groups.index")


@bp.post("/brand-groups/new")
def create():
    name = svc.clean_text(request.form.get("name", ""), 120)
    if not name:
        flash("Give the brand group a name.", "warning")
        return redirect(url_for("groups.index", new="1"))
    group_id = svc.create_group(
        name=name,
        primary_domain=request.form.get("primary_domain", ""),
        category=request.form.get("category", ""),
        notes=request.form.get("notes", ""),
    )
    page_ids = svc.id_list(request.form.getlist("page_ids"))
    added = svc.add_pages_to_group(group_id, page_ids)
    flash(
        f"Created {name}" + (f" with {added} page(s)." if added else "."),
        "success",
    )
    return redirect(url_for("groups.detail", group_id=group_id))


@bp.post("/brand-groups/<int:group_id>/edit")
def edit(group_id: int):
    if svc.get_group(group_id) is None:
        flash("That brand group no longer exists.", "error")
        return redirect(url_for("groups.index"))
    svc.update_group(
        group_id,
        name=request.form.get("name", ""),
        primary_domain=request.form.get("primary_domain", ""),
        category=request.form.get("category", ""),
        notes=request.form.get("notes", ""),
    )
    page_ids = svc.id_list(request.form.getlist("page_ids"))
    added = svc.add_pages_to_group(group_id, page_ids)
    flash("Saved." + (f" Added {added} page(s)." if added else ""), "success")
    return redirect(_back(group_id))


@bp.post("/brand-groups/<int:group_id>/delete")
def delete(group_id: int):
    name = svc.delete_group(group_id)
    flash(
        f"Deleted {name}." if name else "That brand group no longer exists.",
        "success" if name else "warning",
    )
    return redirect(url_for("groups.index"))


@bp.post("/brand-groups/<int:group_id>/pages/add")
def add_pages(group_id: int):
    page_ids = svc.id_list(request.form.getlist("page_ids"))
    if not page_ids:
        flash("Pick at least one page to add.", "warning")
        return redirect(_back(group_id))
    added = svc.add_pages_to_group(group_id, page_ids)
    flash(
        f"Added {added} page(s)." if added
        else "Those pages are already in this group.",
        "success" if added else "warning",
    )
    return redirect(_back(group_id))


@bp.post("/brand-groups/<int:group_id>/pages/<int:page_id>/remove")
def remove_page(group_id: int, page_id: int):
    svc.remove_page_from_group(group_id, page_id)
    flash("Page ungrouped.", "success")
    return redirect(_back(group_id))


@bp.post("/brand-groups/<int:group_id>/pages/track")
def track_pages(group_id: int):
    page_ids = svc.id_list(request.form.getlist("page_ids"))
    tracked = request.form.get("tracked") == "1"
    count = svc.set_page_tracked(page_ids, tracked)
    flash(
        f"{count} page(s) {'tracked' if tracked else 'untracked'}." if count
        else "Nothing to change.",
        "success" if count else "warning",
    )
    return redirect(_back(group_id))


@bp.post("/brand-groups/<int:group_id>/pages/retrack")
def retrack_pages(group_id: int):
    page_ids = svc.id_list(request.form.getlist("page_ids"))
    if not page_ids:
        flash("Nothing to re-track.", "warning")
        return redirect(_back(group_id))

    from .queue import create_scan_job

    group = svc.get_group(group_id) or {}
    result = create_scan_job(page_ids, label=str(group.get("name") or "brand group"))
    if result["job_id"]:
        flash(f"Job #{result['job_id']} queued: {len(result['queued'])} page(s).", "success")
        return redirect(url_for("queue.queue_index"))
    if result["reason"] == "already_queued":
        flash("Those pages are already queued or running.", "warning")
    elif result["reason"] == "no_meta_page_id":
        flash(
            "Those page records have no Meta page id, so there is nothing to open "
            "in the Ad Library. Add them again from their Ad Library URL.",
            "warning",
        )
    else:
        flash("Could not queue a scan for those pages.", "warning")
    return redirect(_back(group_id))


__all__ = ["bp"]
