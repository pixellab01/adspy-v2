"""/health — the one endpoint that must never break.

The launcher polls it to decide whether the server came up, so it stays cheap
(a few COUNTs) and never raises: a database problem is reported as
``ok: false`` with an HTTP 503, not a stack trace.

SERVER MODE: /health stays public (the launcher and the reverse proxy probe it
without a cookie) but an anonymous caller learns exactly two things — ``ok`` and
``status``. No database path, no dataset, no counts, no version, no error text.
A logged-in session gets the full payload, same as local mode.
"""

from __future__ import annotations

import sqlite3

from flask import Blueprint, current_app, jsonify

from .. import auth, dataset, db
from ..time_utils import utc_now

bp = Blueprint("health", __name__)


def _count(sql: str) -> int | None:
    try:
        row = db.fetch_one(sql)
    except sqlite3.Error:
        return None
    return int(row[0]) if row else 0


def _public_health():
    """The anonymous server-mode answer: is it up, and nothing else."""
    try:
        db.get_db().execute("SELECT 1").fetchone()
    except sqlite3.Error:
        return jsonify({"ok": False, "status": "unavailable"}), 503
    return jsonify({"ok": True, "status": "ok"})


@bp.get("/health")
def health():
    if current_app.config.get("SERVER_MODE") and not auth.is_authenticated():
        return _public_health()

    payload = {
        "status": "ok",
        "app": current_app.config["APP_NAME"],
        "version": current_app.config["VERSION"],
        "port": current_app.config["PORT"],
        "time": utc_now(),
        # The ACTIVE dataset's file, resolved now — `adspy2 status` parses it.
        "database": db.current_database_path(),
    }
    info = dataset.describe()
    payload["dataset"] = {
        "active": info["name"],
        "label": info["label"],
        "switchable": info["switchable"],
        "frozen": info["frozen"],
        "paths": info["paths"],
        "new_exists": info["new_exists"],
    }
    payload["active_dataset"] = info["name"]

    try:
        conn = db.get_db()
        payload["migrations"] = db.applied_migrations(conn)
        payload["journal_mode"] = str(
            conn.execute("PRAGMA journal_mode").fetchone()[0]
        )
    except sqlite3.Error as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"database unavailable: {exc}",
                    "code": "DB_UNAVAILABLE",
                    "result": payload,
                }
            ),
            503,
        )

    payload["counts"] = {
        "pages": _count("SELECT COUNT(*) FROM pages"),
        "ads": _count("SELECT COUNT(*) FROM ads"),
        "ads_active": _count("SELECT COUNT(*) FROM ads WHERE status='active'"),
        "products": _count("SELECT COUNT(*) FROM products"),
        "jobs_pending": _count("SELECT COUNT(*) FROM jobs WHERE status='pending'"),
        "jobs_running": _count(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('claimed','running')"
        ),
    }
    # Flat mirror so shell scripts can grep without a JSON parser.
    payload["pages"] = payload["counts"]["pages"]
    return jsonify({"ok": True, "result": payload})
