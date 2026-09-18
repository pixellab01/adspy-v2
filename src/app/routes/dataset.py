"""The dataset switch over HTTP.

    POST /settings/dataset      form field dataset=old|new  -> flash + redirect
    POST /settings/dataset/create   build data/adspy2-new.sqlite3 without switching
    GET  /api/dataset           {"ok": true, "result": describe()}

POST only for anything that changes state: a GET must 405 so a prefetching
link, a bookmark or a browser restoring tabs can never flip the live dataset.
The confirmation dialog is the form's ``data-confirm`` attribute in base.html
(static/app.js asks before submitting).
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from flask import Blueprint, flash, jsonify, redirect, request, url_for

from .. import dataset

log = logging.getLogger("adspy2.dataset")

bp = Blueprint("dataset", __name__)


def _back() -> str:
    """Same-origin referrer or the Settings card. Never an off-site redirect."""
    ref = str(request.referrer or "")
    if ref:
        parts = urlsplit(ref)
        if parts.netloc == request.host and parts.path:
            return parts.path + (f"?{parts.query}" if parts.query else "")
    return url_for("settings.settings_page", section="dataset")


@bp.post("/settings/dataset")
def switch_dataset():
    name = str(request.form.get("dataset") or "").strip().lower()
    try:
        info = dataset.switch(name)
    except dataset.DatasetError as exc:
        flash(exc.message, "warning")
        return redirect(_back())
    if info["name"] == "new":
        flash(
            "Switched to NEW DATA. The extension needs no change — the same token now "
            f"scans into {info['path']}. OLD DATA stays exactly as it was.",
            "success",
        )
    else:
        flash(
            "Switched to OLD DATA. Browse and annotate freely; scans are refused here "
            "until you switch back to NEW DATA.",
            "success",
        )
    return redirect(_back())


@bp.post("/settings/dataset/create")
def create_dataset():
    """One-time: build the NEW file (schema + carried-over token/keys) without
    making it active, so the owner can look before leaping."""
    if not dataset.switchable():
        flash("This process is pinned to one database (ADSPY2_DB_PATH).", "warning")
        return redirect(_back())
    if dataset.describe()["new_exists"]:
        flash("NEW DATA already exists. Nothing was changed.", "success")
        return redirect(_back())
    try:
        path = dataset.create_new_dataset()
    except Exception as exc:  # noqa: BLE001 - surfaced to the owner, never swallowed
        log.warning("could not create NEW dataset: %r", exc)
        flash(f"Could not create NEW DATA: {exc}", "error")
        return redirect(_back())
    flash(f"NEW DATA created at {path} (schema only; worker token and API keys copied). "
          "Switch to it when you are ready.", "success")
    return redirect(_back())


@bp.get("/api/dataset")
def api_dataset():
    return jsonify({"ok": True, "result": dataset.describe()})


__all__ = ["bp"]
