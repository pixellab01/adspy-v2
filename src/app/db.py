"""SQLite access for AdSpy v2.

Ported from meta_main14/services/db.py — the connection discipline there was
right and is kept verbatim in spirit:

    WAL journal, NORMAL synchronous, busy_timeout, foreign_keys ON,
    isolation_level=None (we drive transactions ourselves),
    row_factory = sqlite3.Row, and exactly ONE writer process.

What is deliberately NOT ported: ``modernize_schema`` and its auto-diff/backup
machinery. v2 uses numbered migration files under ``migrations/`` and a
``schema_migrations`` bookkeeping table. Never patch the schema from Python.

Public API (fixed — other modules code against these signatures):

    get_db()                         -> sqlite3.Connection
    current_database_path()          -> str   (the ACTIVE dataset's file)
    connect_readonly(path)           -> sqlite3.Connection  (?mode=ro, never writes)
    transaction(mode="IMMEDIATE")    -> contextmanager yielding the connection
    fetch_all(sql, params=())        -> list[sqlite3.Row]
    fetch_one(sql, params=())        -> sqlite3.Row | None
    execute(sql, params=())          -> sqlite3.Cursor
    run_migrations(conn)             -> list[str]   (names applied this call)
    connect(path)                    -> sqlite3.Connection  (scripts/tests)
    close_db(), init_app(app)
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import config as app_config
from .time_utils import utc_now

# Connections held outside a Flask application context (scripts, tests, worker
# threads). Inside a request we use flask.g so teardown closes it for us.
_local = threading.local()

MIGRATIONS_TABLE = "schema_migrations"

# Files this process has already migrated. Migrations used to run exactly once,
# at create_app(), on the boot-time path — which is fine with one file and a
# silent disaster with two: the NEW dataset, first opened mid-process after a
# switch, would have no schema at all. ``ensure_migrated`` runs them on first
# open of each distinct path instead (one cheap SELECT per file per process).
_MIGRATED_PATHS: set[str] = set()
_MIGRATE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# connection
# ---------------------------------------------------------------------------
def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with the v2 pragmas applied."""
    conn = sqlite3.connect(
        str(path),
        timeout=app_config.SQLITE_TIMEOUT_SECONDS,
        isolation_level=None,           # explicit BEGIN/COMMIT, no implicit txns
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute(f"PRAGMA busy_timeout = {app_config.SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -32000")
    return conn


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    """Open an EXISTING file through a ``?mode=ro`` URI. This is how the
    inactive dataset is ever touched (row counts on the Settings card, the
    carry-over read when NEW is created): SQLite itself refuses every write,
    so a bug here cannot become a lost row. Raises ``sqlite3.OperationalError``
    if the file does not exist — a read-only open must never create one."""
    target = Path(str(path)).expanduser()
    if not target.is_file():
        raise sqlite3.OperationalError(f"no such database file: {target}")
    conn = sqlite3.connect(
        f"file:{target}?mode=ro",
        uri=True,
        timeout=app_config.SQLITE_TIMEOUT_SECONDS,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {app_config.SQLITE_BUSY_TIMEOUT_MS}")
    return conn


def current_database_path() -> str:
    """The file the ACTIVE dataset lives in, resolved now.

        app context, switchable  -> follow the sidecar (re-read on every call,
                                    so a switch takes effect on the next request)
        app context, pinned      -> current_app.config["DATABASE"]
        no app context           -> config.db_path()

    Every reader of the database path goes through here; nothing else in
    ``app/`` may read ``config["DATABASE"]`` directly, or it would keep naming
    the boot-time file after a switch.
    """
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            cfg = current_app.config
            if cfg.get("DATASET_SWITCHABLE"):
                name = app_config.read_active_dataset(cfg.get("DATASET_FILE"))
                return app_config.dataset_db_path(name, cfg.get("DATASET_PATHS"))
            return str(cfg["DATABASE"])
    except ImportError:                                   # pragma: no cover
        pass
    return app_config.db_path()


def _database_path() -> str:
    """Kept as an alias for older callers."""
    return current_database_path()


def ensure_migrated(conn: sqlite3.Connection, path: str, fresh: bool = False) -> list[str]:
    """Run the migrations the first time this process opens ``path`` — or
    again when ``fresh`` says the file did not exist a moment ago (someone
    deleted it under a running server; the cache must not hide that).
    Honours ``RUN_MIGRATIONS`` (tests that build the schema by hand set it
    False). Returns the names applied, [] when nothing was needed."""
    key = str(path)
    if key in _MIGRATED_PATHS and not fresh:
        return []
    with _MIGRATE_LOCK:
        if key in _MIGRATED_PATHS:                        # pragma: no cover
            return []
        run = True
        try:
            from flask import current_app, has_app_context

            if has_app_context():
                run = bool(current_app.config.get("RUN_MIGRATIONS", True))
        except ImportError:                               # pragma: no cover
            pass
        applied = run_migrations(conn) if run else []
        _MIGRATED_PATHS.add(key)
        return applied


def get_db() -> sqlite3.Connection:
    """Per-request (or per-thread) connection with ``row_factory=sqlite3.Row``.

    Inside an app context the FIRST call pins the file for the whole context
    (``g._adspy_db_path``): one request can never straddle two datasets even if
    the sidecar is rewritten while it runs. The next context re-resolves."""
    try:
        from flask import g, has_app_context
    except ImportError:                                   # pragma: no cover
        has_app_context = None  # type: ignore[assignment]

    if has_app_context is not None and has_app_context():
        # Request-scoped: owned by flask.g and closed by teardown_appcontext.
        # Deliberately NOT shared with the thread-local cache below, or a
        # torn-down context would hand a closed handle to the next one.
        conn = g.get("_adspy_db")
        if conn is None:
            path = g.get("_adspy_db_path") or current_database_path()
            fresh = _is_missing_file(path)
            _ensure_parent(path)
            conn = connect(path)
            g._adspy_db = conn
            g._adspy_db_path = path
            ensure_migrated(conn, path, fresh=fresh)
        return conn

    return _open_for_path(current_database_path())


def _open_for_path(path: str) -> sqlite3.Connection:
    """Thread-local connection cache for use outside a Flask app context
    (scripts, background threads, tests). Keyed by path so a switched test
    database is never served from a stale handle."""
    cached_path = getattr(_local, "path", None)
    conn = getattr(_local, "conn", None)
    if conn is not None and cached_path == path and _is_usable(conn):
        return conn
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:                             # pragma: no cover
            pass
    fresh = _is_missing_file(path)
    _ensure_parent(path)
    conn = connect(path)
    _local.conn = conn
    _local.path = path
    ensure_migrated(conn, path, fresh=fresh)
    return conn


def _is_missing_file(path: str) -> bool:
    if path == ":memory:" or path.startswith("file:"):
        return False
    try:
        return not Path(path).expanduser().is_file()
    except OSError:                                       # pragma: no cover
        return False


def _is_usable(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
        return True
    except sqlite3.Error:
        return False


def _ensure_parent(path: str) -> None:
    if path == ":memory:" or path.startswith("file:"):
        return
    parent = Path(path).expanduser().parent
    if str(parent):
        parent.mkdir(parents=True, exist_ok=True)


def close_db(_: BaseException | None = None) -> None:
    """Flask teardown hook; also closes the thread-local handle when used bare."""
    try:
        from flask import g, has_app_context

        if has_app_context():
            conn = g.pop("_adspy_db", None)
            g.pop("_adspy_db_path", None)
            if conn is not None:
                conn.close()
            return
    except ImportError:                                   # pragma: no cover
        pass

    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.path = None


# ---------------------------------------------------------------------------
# transactions — single writer, BEGIN IMMEDIATE
# ---------------------------------------------------------------------------
@contextmanager
def transaction(mode: str = "IMMEDIATE") -> Iterator[sqlite3.Connection]:
    """Write transaction. ``BEGIN IMMEDIATE`` takes the write lock up front so
    two writers fail fast with 'database is locked' instead of deadlocking
    halfway through. Nesting is a no-op passthrough (the outermost owns the
    commit), which keeps ingest free to call helpers that also want a txn."""
    conn = get_db()
    if conn.in_transaction:
        yield conn
        return

    conn.execute(f"BEGIN {mode}")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:                             # pragma: no cover
            pass
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# query helpers — all return sqlite3.Row (dict-like AND tuple-like)
# ---------------------------------------------------------------------------
def fetch_all(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return list(get_db().execute(sql, tuple(params)).fetchall())


def fetch_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return get_db().execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    """Run a statement. Use ``cursor.lastrowid`` / ``cursor.rowcount``."""
    return get_db().execute(sql, tuple(params))


def executemany(sql: str, seq_of_params: Iterable[Iterable[Any]]) -> sqlite3.Cursor:
    return get_db().executemany(sql, [tuple(p) for p in seq_of_params])


# ---------------------------------------------------------------------------
# migrations — numbered .sql files, applied once, in name order
# ---------------------------------------------------------------------------
def _migrations_dir() -> Path:
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            return Path(str(current_app.config["MIGRATIONS_DIR"]))
    except ImportError:                                   # pragma: no cover
        pass
    return app_config.MIGRATIONS_DIR


def migration_files(directory: str | Path | None = None) -> list[Path]:
    folder = Path(directory) if directory else _migrations_dir()
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.sql") if p.is_file())


def applied_migrations(conn: sqlite3.Connection) -> list[str]:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (MIGRATIONS_TABLE,),
    ).fetchone()
    if not row:
        return []
    return [
        str(r[0])
        for r in conn.execute(
            f"SELECT name FROM {MIGRATIONS_TABLE} ORDER BY name"
        ).fetchall()
    ]


def run_migrations(
    conn: sqlite3.Connection,
    directory: str | Path | None = None,
) -> list[str]:
    """Apply every not-yet-applied migration file. Idempotent: a second call
    on the same database applies nothing and returns []."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} (
            name       TEXT PRIMARY KEY,
            checksum   TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    done = set(applied_migrations(conn))
    applied: list[str] = []

    for path in migration_files(directory):
        if path.name in done:
            continue
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        # NOTE: sqlite3.executescript() commits any pending transaction first,
        # so wrapping this in BEGIN IMMEDIATE would be theatre. Instead every
        # migration file must be written so a re-run is harmless
        # (CREATE TABLE IF NOT EXISTS ...): if the bookkeeping INSERT below
        # never happens, the next run simply replays the file.
        conn.executescript(sql)
        conn.execute(
            f"INSERT INTO {MIGRATIONS_TABLE}(name, checksum, applied_at) VALUES(?,?,?)",
            (path.name, checksum, utc_now()),
        )
        applied.append(path.name)

    return applied


# ---------------------------------------------------------------------------
# flask wiring
# ---------------------------------------------------------------------------
def init_app(app) -> None:
    app.teardown_appcontext(close_db)
