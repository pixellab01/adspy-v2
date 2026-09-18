"""Configuration constants for AdSpy v2.

No secrets live here. In LOCAL MODE (the default) there is no login, no users,
no roles and no ``workspace_id`` anywhere in this app (docs/00-decisions.md #4)
— the only security boundary is that we bind to 127.0.0.1 and nothing else.

SERVER MODE (``ADSPY2_SERVER_MODE=1``) is for a VPS behind a reverse proxy:
gunicorn STILL binds 127.0.0.1, the proxy terminates HTTPS, and app/auth.py
puts a single-admin login in front of every screen. Its two secrets come from
the environment only — ``ADSPY2_ADMIN_PASSWORD`` and ``ADSPY2_SECRET_KEY`` —
and ``create_app`` refuses to boot without them. See app/auth.py.

The database path is resolved in this order:
    1. ``ADSPY2_DB_PATH`` environment variable  (tests set this to a temp file).
       When it is set the dataset is PINNED: no switching, one file, full stop.
    2. the active DATASET — ``old`` or ``new`` — read from a one-line sidecar
       file (``data/active_dataset.txt``, overridable with
       ``ADSPY2_DATASET_FILE``). ``old`` is ``data/adspy2.sqlite3`` (everything
       scraped before the rebuild; never written by a new scan), ``new`` is
       ``data/adspy2-new.sqlite3`` (the fresh space for the rebuilt tool).
       A missing, empty or unreadable sidecar means ``old``.

The sidecar lives OUTSIDE both database files on purpose: a setting inside
either file would be lost the moment you switched away from it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

APP_NAME = "AdSpy v2"
VERSION = "2.0.0"

# --- network -----------------------------------------------------------------
# Loopback only, in BOTH modes. Never change this to 0.0.0.0: local mode has no
# auth layer at all, and in server mode exposure is the reverse proxy's job
# (Caddy: automatic HTTPS) — gunicorn itself is never reachable from outside.
HOST = "127.0.0.1"
DEFAULT_PORT = 4022              # v1 keeps 4021; never touch it.
V1_PORT = 4021

PORT_ENV = "ADSPY2_PORT"
SERVER_MODE_ENV = "ADSPY2_SERVER_MODE"
ADMIN_PASSWORD_ENV = "ADSPY2_ADMIN_PASSWORD"
SECRET_KEY_ENV = "ADSPY2_SECRET_KEY"
PUBLIC_URL_ENV = "ADSPY2_PUBLIC_URL"

_TRUTHY = ("1", "true", "yes", "on")


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def server_mode_from_env() -> bool:
    """``ADSPY2_SERVER_MODE=1`` (or true/yes/on). Anything else is local mode."""
    return env_flag(SERVER_MODE_ENV)


def port_from_env() -> int:
    """``ADSPY2_PORT`` or 4022. Garbage, out-of-range and v1's 4021 are refused
    loudly: silently falling back would start the app somewhere the owner did
    not ask for, and 4021 belongs to v1."""
    raw = os.environ.get(PORT_ENV, "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{PORT_ENV}={raw!r} is not a number") from None
    if not 1024 <= value <= 65535:
        raise ValueError(f"{PORT_ENV}={value} must be between 1024 and 65535")
    if value == V1_PORT:
        raise ValueError(f"{PORT_ENV}={value} is v1's port - pick another")
    return value


def _public_url(default: str) -> str:
    """``ADSPY2_PUBLIC_URL`` — the address the OWNER types (https://ads.example.com)
    when the app sits behind a proxy. Only used for display (the Queue screen
    tells the extension where the dashboard is)."""
    raw = os.environ.get(PUBLIC_URL_ENV, "").strip().rstrip("/")
    if raw.startswith(("https://", "http://")):
        return raw
    return default


PORT = port_from_env()
BASE_URL = _public_url(f"http://{HOST}:{PORT}")

# --- paths -------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
MIGRATIONS_DIR = BASE_DIR / "migrations"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

DB_PATH_ENV = "ADSPY2_DB_PATH"
DEFAULT_DB_PATH = DATA_DIR / "adspy2.sqlite3"

# --- datasets: OLD (pre-rebuild, read-only for scans) and NEW (fresh) ---------
DATASET_FILE_ENV = "ADSPY2_DATASET_FILE"
DEFAULT_DATASET_FILE = DATA_DIR / "active_dataset.txt"
DATASETS: tuple[str, ...] = ("old", "new")
DEFAULT_DATASET = "old"
DATASET_PATHS: dict[str, Path] = {
    "old": DEFAULT_DB_PATH,
    "new": DATA_DIR / "adspy2-new.sqlite3",
}
DATASET_LABELS: dict[str, str] = {"old": "OLD DATA", "new": "NEW DATA"}

# v1 database — READ ONLY, and only ever opened through a ?mode=ro URI by
# scripts/import_v1.py. The app itself never touches it.
V1_DB_PATH = BASE_DIR.parent / "meta_main14" / "data" / "pixellab_adspy.sqlite3"

# --- sqlite ------------------------------------------------------------------
SQLITE_TIMEOUT_SECONDS = 15
SQLITE_BUSY_TIMEOUT_MS = 15000

# --- ingest ------------------------------------------------------------------
# Ads captured inside this window are never deactivated by reconciliation.
RECONCILE_GRACE_SECONDS = 600
# A page-scan batch whose pageId is not numeric \d{5,} is rejected outright.
NUMERIC_PAGE_ID_MIN_DIGITS = 5
# Reconciliation only ever runs for these final outcomes on a page scan.
FINAL_OUTCOMES = ("complete", "exhausted", "empty")
VALID_OUTCOMES = ("complete", "exhausted", "empty", "partial", "blocked", "failed")

# --- queue / lease -----------------------------------------------------------
LEASE_TTL_SECONDS = 300          # renewed by every /batch and /status call
WORKER_HEARTBEAT_SECONDS = 30
JOB_MAX_RETRIES = 3

# --- extension pacing (mirrored in the extension; server-side reference) ------
BATCH_MAX_ADS = 25
BATCH_MAX_SECONDS = 8
MAX_PAGES_PER_HOUR = 12
MAX_PAGES_PER_DAY = 100  # informational only; the extension enforces pacing


def dataset_pinned() -> bool:
    """True when ``ADSPY2_DB_PATH`` names one exact file (tests, scripts).
    A pinned process never switches datasets."""
    return bool(os.environ.get(DB_PATH_ENV, "").strip())


def dataset_file() -> Path:
    """Where the active-dataset sidecar lives (env override or data/)."""
    override = os.environ.get(DATASET_FILE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return DEFAULT_DATASET_FILE


def read_active_dataset(path: str | Path | None = None) -> str:
    """``'old'`` or ``'new'``. Never raises: a missing, empty or garbage sidecar
    is ``'old'`` (logged once per distinct bad value), and the file is never
    created here — only ``app.dataset.switch`` writes it."""
    target = Path(path) if path else dataset_file()
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return DEFAULT_DATASET
    value = raw.strip().lower()
    if value in DATASETS:
        return value
    if value and value not in _WARNED_SIDECAR_VALUES:
        _WARNED_SIDECAR_VALUES.add(value)
        logging.getLogger("adspy2.dataset").warning(
            "sidecar %s says %r (not one of %s) — using %r",
            target, value[:40], list(DATASETS), DEFAULT_DATASET,
        )
    return DEFAULT_DATASET


_WARNED_SIDECAR_VALUES: set[str] = set()


def dataset_db_path(name: str, paths: dict | None = None) -> str:
    table = paths or DATASET_PATHS
    return str(Path(str(table[name])).expanduser())


def db_path() -> str:
    """Absolute path of the v2 SQLite file. Contract unchanged: the env override
    wins; otherwise it is the active dataset's file."""
    override = os.environ.get(DB_PATH_ENV, "").strip()
    if override:
        return str(Path(override).expanduser())
    return dataset_db_path(read_active_dataset())


def default_config() -> dict:
    """Base Flask config dict. ``create_app`` overlays caller overrides on top."""
    return {
        "APP_NAME": APP_NAME,
        "VERSION": VERSION,
        "HOST": HOST,
        "PORT": PORT,
        # Server mode: read from the environment here, and ALSO settable through
        # create_app({...}) so a test can turn it on without touching os.environ.
        # The two secrets are NOT copied into this dict — app/auth.py reads them
        # from the environment (or from the caller's override) at boot and keeps
        # only a digest of the password.
        "SERVER_MODE": server_mode_from_env(),
        "DATABASE": db_path(),
        "DATASET_FILE": str(dataset_file()),
        "DATASET_PATHS": {name: str(path) for name, path in DATASET_PATHS.items()},
        "DATASET_SWITCHABLE": not dataset_pinned(),
        "MIGRATIONS_DIR": str(MIGRATIONS_DIR),
        "RUN_MIGRATIONS": True,
        "RECONCILE_GRACE_SECONDS": RECONCILE_GRACE_SECONDS,
        "LEASE_TTL_SECONDS": LEASE_TTL_SECONDS,
        "JOB_MAX_RETRIES": JOB_MAX_RETRIES,
        "JSON_SORT_KEYS": False,
    }
