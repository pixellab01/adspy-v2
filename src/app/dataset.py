"""Two datasets, one switch.

The owner asked to keep everything scraped so far visible as OLD DATA and to
get a fresh NEW DATA space to test the rebuilt tool with — with a toggle at the
top to look at either. The decision (docs/00-decisions.md, "Datasets"):

    OLD  data/adspy2.sqlite3       everything scraped before the rebuild.
                                   Browsable, editable by the owner (shortlist,
                                   hide, notes, groups, on-demand transcription)
                                   but NEVER written by a new scan.
    NEW  data/adspy2-new.sqlite3   schema only at birth; every scan from now on
                                   lands here.

Which one is live is a one-line sidecar file, ``data/active_dataset.txt``,
holding ``old`` or ``new``. It lives outside both databases because a setting
inside either file would vanish the moment you switched away from it. Missing
sidecar means ``old``, so nothing changes until the owner switches.

This module is the ONLY writer of the sidecar and the only place that creates
the NEW file. ``app/db.py::current_database_path`` reads the sidecar on every
request; ``app/routes/dataset.py`` is the HTTP face; ``adspy2 dataset`` the CLI.

THE FREEZE. ``scan_writes_allowed()`` is False whenever the app is switchable
and OLD is active. It is checked at three depths so no single bypass exists:
``job_service.create_job`` (queueing), ``jobs.claim`` (the extension idles with
``reason: dataset_frozen``) and ``ingest.ingest_batch`` (where the batch write
lands — nothing is written, not even the receipt). A pinned process (tests,
``ADSPY2_DB_PATH`` scripts) is never frozen: the freeze is about the live
two-file mode, and the tests exercise it through a switchable app.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from . import config as app_config
from . import db
from .time_utils import utc_now

log = logging.getLogger("adspy2.dataset")

# Settings rows copied from the active file into a freshly created NEW file.
# The worker token MUST travel or every extension call fails BAD_WORKER_TOKEN
# after the switch (a fresh file would mint a different token on first read).
# The provider keys travel so the owner does not paste them twice.
# ``queue_paused`` deliberately does not: a new dataset starts running.
CARRY_OVER_KEYS: tuple[str, ...] = (
    "worker_token",
    "gemini_api_key",
    "groq_api_key",
    "sarvam_api_key",
    "transcription_provider",
)

# Tables whose rows are bookkeeping, not data — a "schema only" NEW file may
# hold rows here and nowhere else.
BOOKKEEPING_TABLES = frozenset({"schema_migrations", "settings"})

BUSY_STATUSES = ("claimed", "running")


class DatasetError(Exception):
    """Typed refusal: ``code`` is stable for callers, ``status`` is the HTTP
    answer the route gives."""

    code = "DATASET_ERROR"
    status = 400

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status


# ---------------------------------------------------------------------------
# resolution — all read-only, none of it opens a database
# ---------------------------------------------------------------------------
def _config() -> dict[str, Any]:
    """Flask config when there is one; module defaults otherwise (CLI)."""
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            return current_app.config
    except ImportError:  # pragma: no cover
        pass
    return {
        "DATASET_SWITCHABLE": not app_config.dataset_pinned(),
        "DATASET_FILE": str(app_config.dataset_file()),
        "DATASET_PATHS": {k: str(v) for k, v in app_config.DATASET_PATHS.items()},
        "DATABASE": app_config.db_path(),
    }


def switchable() -> bool:
    return bool(_config().get("DATASET_SWITCHABLE"))


def sidecar_path() -> Path:
    return Path(str(_config().get("DATASET_FILE") or app_config.dataset_file()))


def paths() -> dict[str, str]:
    table = _config().get("DATASET_PATHS") or {}
    return {
        name: app_config.dataset_db_path(name, table) if name in table else
        app_config.dataset_db_path(name)
        for name in app_config.DATASETS
    }


def active() -> str:
    """``'old'`` | ``'new'``. A pinned process reports ``old`` unless its pinned
    file happens to be the NEW dataset's path."""
    cfg = _config()
    if cfg.get("DATASET_SWITCHABLE"):
        return app_config.read_active_dataset(cfg.get("DATASET_FILE"))
    pinned = str(cfg.get("DATABASE") or "")
    for name, path in paths().items():
        if pinned and Path(pinned) == Path(path):
            return name
    return app_config.DEFAULT_DATASET


def other(name: str | None = None) -> str:
    name = name or active()
    return "new" if name == "old" else "old"


def label(name: str) -> str:
    return app_config.DATASET_LABELS.get(name, name.upper())


def scan_writes_allowed() -> bool:
    """False iff the app is switchable AND OLD DATA is active. New scans go to
    NEW only; OLD is browse-and-annotate."""
    return not (switchable() and active() == "old")


def frozen_message() -> str:
    return (
        "OLD DATA is read-only for scans — switch to NEW DATA (topbar or "
        "Settings › Data version) before tracking or scanning."
    )


def describe() -> dict[str, Any]:
    """Everything a template or /health needs. Never raises, never opens a
    database: it runs inside the context processor, on the 404 page too."""
    name = active()
    table = paths()
    try:
        new_exists = Path(table["new"]).is_file()
    except OSError:  # pragma: no cover
        new_exists = False
    return {
        "name": name,
        "label": label(name),
        "other": other(name),
        "other_label": label(other(name)),
        "path": table[name],
        "paths": table,
        "switchable": switchable(),
        "frozen": not scan_writes_allowed(),
        "new_exists": new_exists,
        "sidecar": str(sidecar_path()),
    }


# ---------------------------------------------------------------------------
# read-only facts about either file (Settings card)
# ---------------------------------------------------------------------------
COUNT_TABLES: tuple[tuple[str, str], ...] = (
    ("pages", "SELECT COUNT(*) FROM pages"),
    ("ads", "SELECT COUNT(*) FROM ads"),
    ("ads_active", "SELECT COUNT(*) FROM ads WHERE status='active'"),
    ("products", "SELECT COUNT(*) FROM products"),
    ("transcripts", "SELECT COUNT(*) FROM transcripts"),
    ("jobs", "SELECT COUNT(*) FROM jobs"),
)


def facts(name: str) -> dict[str, Any]:
    """Path, existence, size and row counts of one dataset. The inactive file
    is opened ``?mode=ro``; the active one goes through the request handle."""
    path = Path(paths()[name])
    out: dict[str, Any] = {
        "name": name,
        "label": label(name),
        "path": str(path),
        "exists": path.is_file(),
        "size_mb": 0.0,
        "counts": {},
        "active": name == active(),
    }
    if not out["exists"]:
        return out
    try:
        out["size_mb"] = round(path.stat().st_size / 1_048_576, 1)
    except OSError:  # pragma: no cover
        pass

    conn: sqlite3.Connection | None = None
    close = False
    try:
        if out["active"]:
            conn = db.get_db()
        else:
            conn = db.connect_readonly(path)
            close = True
        for key, sql in COUNT_TABLES:
            try:
                row = conn.execute(sql).fetchone()
                out["counts"][key] = int(row[0]) if row else 0
            except sqlite3.Error:
                out["counts"][key] = None
    except sqlite3.Error as exc:
        log.warning("could not read %s dataset facts: %s", name, type(exc).__name__)
    finally:
        if close and conn is not None:
            conn.close()
    return out


def busy_jobs() -> int:
    """Claimed/running jobs in the ACTIVE file — a lease-holding extension must
    never find its job table swapped under it. Goes through the request handle:
    the active file is the one this context is pinned to anyway."""
    if not Path(paths()[active()]).is_file():
        return 0
    try:
        row = db.get_db().execute(
            f"SELECT COUNT(*) FROM jobs WHERE status IN ({','.join('?' * len(BUSY_STATUSES))})",
            BUSY_STATUSES,
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


# ---------------------------------------------------------------------------
# creation of the NEW file (schema + carried-over settings, no data)
# ---------------------------------------------------------------------------
def create_new_dataset(source: str | Path | None = None) -> str:
    """Build ``data/adspy2-new.sqlite3``: migrations, then the CARRY_OVER_KEYS
    rows copied from ``source`` (default: the OLD file, opened read-only).
    Idempotent: an existing NEW file is left alone. On any failure the
    half-built file is removed so it can never be picked up later."""
    table = paths()
    target = Path(table["new"])
    if target.is_file():
        return str(target)
    source_path = Path(str(source or table["old"]))

    target.parent.mkdir(parents=True, exist_ok=True)
    conn: sqlite3.Connection | None = None
    try:
        conn = db.connect(target)
        db.run_migrations(conn)
        _carry_over_settings(source_path, conn)
        conn.close()
        conn = None
    except Exception:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(f"{target}{suffix}")
            except OSError:
                pass
        raise
    log.info("created NEW dataset at %s (schema + %d carried-over settings)",
             target, len(CARRY_OVER_KEYS))
    return str(target)


def _carry_over_settings(source_path: Path, dest: sqlite3.Connection) -> None:
    if not source_path.is_file():
        return
    src = db.connect_readonly(source_path)              # reads the old file, never writes it
    try:
        rows = src.execute(
            f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(CARRY_OVER_KEYS))})",
            CARRY_OVER_KEYS,
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        src.close()
    now = utc_now()
    dest.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            dest.execute(
                "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES(?,?,?)",
                (str(row["key"]), str(row["value"]), now),
            )
    except sqlite3.Error:
        dest.execute("ROLLBACK")
        raise
    dest.execute("COMMIT")


# ---------------------------------------------------------------------------
# the switch — the only writer of the sidecar
# ---------------------------------------------------------------------------
def switch(name: str) -> dict[str, Any]:
    name = str(name or "").strip().lower()
    if name not in app_config.DATASETS:
        raise DatasetError(
            f"unknown dataset {name!r} — expected one of {list(app_config.DATASETS)}",
            code="DATASET_UNKNOWN", status=400,
        )
    if not switchable():
        raise DatasetError(
            "this process is pinned to one database (ADSPY2_DB_PATH) and cannot switch",
            code="DATASET_PINNED", status=409,
        )
    current = active()
    if name == current:
        return describe()

    busy = busy_jobs()
    if busy:
        raise DatasetError(
            f"{busy} job{'s are' if busy != 1 else ' is'} claimed or running on "
            f"{label(current)} — stop the queue and let the extension finish first",
            code="DATASET_BUSY", status=409,
        )

    if name == "new" and not Path(paths()["new"]).is_file():
        try:
            create_new_dataset()
        except Exception as exc:  # noqa: BLE001 - the half-built file is already gone
            log.warning("could not create NEW dataset: %r", exc)
            raise DatasetError(
                f"could not create NEW DATA: {exc} — nothing was switched",
                code="DATASET_CREATE_FAILED", status=500,
            ) from exc

    _write_sidecar(name)
    log.info("dataset switched %s -> %s", current, name)
    return describe()


def _write_sidecar(name: str) -> None:
    target = sidecar_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(name + "\n", encoding="utf-8")
    os.replace(tmp, target)


__all__ = [
    "CARRY_OVER_KEYS", "DatasetError", "active", "busy_jobs", "create_new_dataset",
    "describe", "facts", "frozen_message", "label", "other", "paths",
    "scan_writes_allowed", "switch", "switchable",
]
