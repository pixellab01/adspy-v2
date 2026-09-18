"""Logs — the diagnostics screen (owner request #6).

    "if any bug or anything comes up, one click and it is saved to the logs,
     and whenever we restart, the logs tell me which bugs happened and what
     still needs solving."

Before this screen existed, ``GET /logs`` was a 404 and templates/base.html
told the owner to go and read ``logs/server.error.log`` himself — a 2.3 MB
never-rotated file, unreachable from the UI, on a machine where the extension
had been silently halted for five days and nothing said so.

WHAT THIS BLUEPRINT OWNS
------------------------
    GET  /logs                                the screen
    POST /logs/capture                        "Save current logs" - one click
    POST /logs/sweep                          re-derive issues now
    POST /logs/note                           record something by hand
    POST /logs/<id>/resolve                   stop it nagging
    POST /logs/<id>/reopen                    it was not fixed after all
    POST /logs/resolve-all                    clear the board
    GET  /logs/snapshots/<id>/download        the saved file
    POST /logs/snapshots/<id>/delete
    POST /api/logs/worker                     the extension ships its buffer

WHY THE WORKER ENDPOINT IS NOT /api/worker/log
----------------------------------------------
docs/03 §4 fixes the worker protocol at five endpoints and two tests ratchet
it (tests/test_worker_api.py, tests/test_end_to_end.py). Diagnostics are not
part of that protocol — they are a sink, and a scrape must never depend on
them — so the route lives under ``/api/logs`` and the five stay five. Same
token, same envelope.

All the thinking about dedupe, retention and where issues come from lives in
app/log_service.py; this file is routes, flashes and redirects.
"""

from __future__ import annotations

import logging
import os
import secrets

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from .. import log_service as ls
from ..job_service import upsert_worker, verify_worker_token

log = logging.getLogger("adspy2.logs")

bp = Blueprint("logs", __name__)
api_bp = Blueprint("logs_api", __name__, url_prefix="/api/logs")

WORKER_TOKEN_HEADER = "X-Worker-Token"
LOG_TAIL_LINES = 120
WORKER_TAIL_LINES = 80


@bp.record_once
def _wire_up(state) -> None:
    """Two things the screen needs from the app itself.

    1. A signed session, so ``flash()`` works — the same guard every other
       screen carries (app/routes/pages.py, app/routes/alerts.py).
    2. The exception hook. It is installed from here rather than from
       app/__init__.py so that adding diagnostics costs the factory exactly one
       line in its module list: if this blueprint is ever dropped, capture goes
       with it and nothing else changes.
    """
    app = state.app
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = (
            os.environ.get("ADSPY2_SECRET_KEY") or secrets.token_hex(32)
        )
    if not ls.install_error_capture(app):                  # pragma: no cover
        log.warning("blinker missing: unhandled server errors will not be logged")


# ---------------------------------------------------------------------------
# the screen
# ---------------------------------------------------------------------------
def _requested_state() -> str:
    wanted = str(request.args.get("state") or ls.STATE_OPEN).strip()
    return wanted if wanted in ls.STATES else ls.STATE_OPEN


def _requested_kind() -> str:
    wanted = str(request.args.get("kind") or "").strip()
    return wanted if wanted in ls.KINDS else ""


def _back() -> str:
    return request.form.get("next") or url_for("logs.index")


@bp.get("/logs")
def index():
    """Open issues first. The sweep runs here, in the request, at most once a
    minute — the same "no daemon" rule the Alerts screen follows."""
    sweep = ls.sweep()
    state = _requested_state()
    kind = _requested_kind()
    counts = ls.issue_counts()

    state_filters = [
        (ls.STATE_OPEN, "Open", url_for("logs.index", state=ls.STATE_OPEN, kind=kind or None), counts["open"]),
        (ls.STATE_RESOLVED, "Resolved", url_for("logs.index", state=ls.STATE_RESOLVED, kind=kind or None), counts["resolved"]),
        (ls.STATE_ALL, "All", url_for("logs.index", state=ls.STATE_ALL, kind=kind or None), counts["total"]),
    ]
    kind_filters = [
        ("", "Everything", url_for("logs.index", state=state), counts["open"]),
    ]
    for name in ls.KINDS:
        amount = counts["by_kind"].get(name, 0)
        if amount or name == kind:
            kind_filters.append(
                (name, ls.KIND_LABELS[name], url_for("logs.index", state=state, kind=name), amount)
            )

    return render_template(
        "logs.html",
        title="Logs",
        active_nav="logs",
        subtitle="Bugs and failures this tool has seen, kept across restarts",
        issues=ls.list_issues(state, kind),
        counts=counts,
        state_filters=state_filters,
        kind_filters=kind_filters,
        current_state=state,
        current_kind=kind,
        snapshots=ls.list_snapshots(),
        worker=ls.worker_summary(),
        worker_lines=ls.worker_lines(WORKER_TAIL_LINES),
        error_log=list(reversed(ls.server_log_tail("server.error.log", LOG_TAIL_LINES))),
        log_files=ls.server_log_facts(),
        sweep_ran=bool(sweep.get("ran")),
        cooldown_seconds=ls.SWEEP_COOLDOWN_SECONDS,
        issue_limit=ls.ISSUE_LIMIT,
        snapshot_limit=ls.SNAPSHOT_LIMIT,
    )


# ---------------------------------------------------------------------------
# one-click capture
# ---------------------------------------------------------------------------
@bp.post("/logs/capture")
def capture():
    """The owner's one click. Sweeps first so the file holds what is true right
    now, writes one .txt, and says where it went."""
    ls.sweep(force=True)
    note = str(request.form.get("note") or "").strip()
    try:
        saved = ls.save_snapshot(note)
    except OSError as exc:
        log.warning("snapshot failed: %r", exc)
        flash(f"Could not write the log file: {exc}", "error")
        return redirect(_back())
    flash(
        f"Saved {saved['filename']} — {saved['open_issues']} open issue(s), "
        f"{round(saved['size_bytes'] / 1024, 1)} KB. It stays in logs/snapshots "
        "after a restart.",
        "success",
    )
    return redirect(_back())


@bp.get("/logs/snapshots/<int:snapshot_id>/download")
def download_snapshot(snapshot_id: int):
    try:
        path, filename = ls.snapshot_file(snapshot_id)
    except ls.LogError as exc:
        flash(str(exc), "error")
        return redirect(url_for("logs.index"))
    return send_file(
        path, mimetype="text/plain; charset=utf-8",
        as_attachment=True, download_name=filename,
    )


@bp.get("/logs/current.txt")
def download_current():
    """The same report the button saves, rendered live and never stored — for
    when you just want to read or paste it without keeping a file."""
    ls.sweep()
    return Response(
        ls.render_snapshot("live view, not saved"),
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="adspy2-logs-now.txt"'},
    )


@bp.post("/logs/snapshots/<int:snapshot_id>/delete")
def delete_snapshot(snapshot_id: int):
    try:
        filename = ls.delete_snapshot(snapshot_id)
    except ls.LogError as exc:
        flash(str(exc), "error")
        return redirect(_back())
    flash(f"Deleted {filename}.", "success")
    return redirect(_back())


# ---------------------------------------------------------------------------
# working the list
# ---------------------------------------------------------------------------
@bp.post("/logs/sweep")
def sweep_now():
    result = ls.sweep(force=True)
    if result["created"]:
        flash(f"{result['created']} new issue(s) found.", "warning")
    else:
        flash("Re-checked jobs, targets, batches and pages. Nothing new.", "success")
    return redirect(_back())


@bp.post("/logs/note")
def add_note():
    title = str(request.form.get("title") or "").strip()
    if not title:
        flash("Write what went wrong first.", "error")
        return redirect(_back())
    result = ls.record_manual_note(title, str(request.form.get("detail") or "").strip())
    flash(
        f"Noted (seen {result['occurrences']}x)." if not result["created"] else "Noted.",
        "success",
    )
    return redirect(_back())


@bp.post("/logs/<int:issue_id>/resolve")
def resolve(issue_id: int):
    note = str(request.form.get("note") or "").strip()
    if ls.resolve_issue(issue_id, note):
        flash("Marked solved. It reopens by itself if it happens again.", "success")
    else:
        flash("That issue was already resolved.", "warning")
    return redirect(_back())


@bp.post("/logs/<int:issue_id>/reopen")
def reopen(issue_id: int):
    if ls.reopen_issue(issue_id):
        flash("Reopened.", "success")
    else:
        flash("That issue is already open.", "warning")
    return redirect(_back())


@bp.post("/logs/resolve-all")
def resolve_everything():
    kind = str(request.form.get("kind") or "").strip()
    updated = ls.resolve_all(kind)
    flash(
        f"{updated} issue(s) marked solved." if updated else "Nothing open.",
        "success" if updated else "warning",
    )
    return redirect(_back())


# ---------------------------------------------------------------------------
# POST /api/logs/worker — the extension's ring buffer, landed
# ---------------------------------------------------------------------------
def _worker_authorised() -> bool:
    """Same rule as ``/api/worker/hello``: the shared token, or the extension
    itself on loopback before the owner has pasted one."""
    supplied = str(request.headers.get(WORKER_TOKEN_HEADER) or "").strip()
    if supplied:
        return verify_worker_token(supplied)
    # SERVER MODE: behind a reverse proxy every request is loopback, so the
    # tokenless first-contact allowance below must not exist (same rule as
    # app/jobs.py::_trusted_first_contact; app/auth.py's gate enforces it too).
    if current_app.config.get("SERVER_MODE"):
        return False
    if request.remote_addr not in (None, "", "127.0.0.1", "::1", "localhost"):
        return False
    origin = str(request.headers.get("Origin") or "").strip()
    return not origin or origin.startswith(
        ("chrome-extension://", "moz-extension://", "http://127.0.0.1", "http://localhost")
    )


@api_bp.post("/worker")
def worker_log():
    """Body: ``{installationId, extensionVersion, state, haltReason, lines:[
    {t, level, msg}]}``. Idempotent — re-shipping the whole 200-line buffer is
    free, so the extension never has to track what it already sent."""
    if not _worker_authorised():
        return jsonify({"ok": False, "error": "missing or invalid worker token",
                        "code": "BAD_WORKER_TOKEN"}), 401
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "a JSON object is required",
                        "code": "BAD_PAYLOAD"}), 400
    # D1. The same envelope carries the worker's state and halt reason, so a
    # halted worker that only ships its buffer still refreshes its registry row.
    upsert_worker(
        payload.get("installationId") or payload.get("workerId"),
        state=str(payload.get("state") or ""),
        extension_version=payload.get("extensionVersion"),
        last_error=payload.get("haltReason"),
    )
    result = ls.record_worker_lines(payload)
    return jsonify({"ok": True, "result": result}), 200


__all__ = ["bp", "api_bp"]
