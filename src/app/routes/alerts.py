"""Alerts screen — v1's "Scaling Alerts", derived instead of daemon-generated.

WHAT THIS IS
------------
v1 (meta_main14/tabs/alerts + services/alerts_service.py) ran a background
sweep thread on the tail of *every* request, wrote rows into ``alerts``, and
optionally pushed each one to Telegram. It produced 1,279 alerts that nobody
ever read. The screen itself is good; the daemon was not.

So v2 keeps v1's screen — the same five filters in the same order, the same
counts, the same "Mark read" / "Mark all read" / "Run sweep now" buttons, the
same six thresholds in the same order with the same labels — and changes only
where the rows come from:

    THE SWEEP RUNS WHEN YOU OPEN /alerts, IN THE REQUEST, AT MOST EVERY
    10 MINUTES. No thread, no daemon, no after_app_request hook.

That is affordable because every fact an alert is made of is already in the
database and each detector is a single query:

    scaling / stopped  page_daily_metrics — the last two readings for a page
                       (falling back to alert_page_baselines, then to
                       pages.prev_active_ads / pages.last_delta)
    new hot page       pages.first_captured_at + pages.active_ads
    product surge      products x ad_products x ads(status='active')

WHY ANYTHING IS PERSISTED AT ALL
--------------------------------
"Read" is a fact about the reader, not about the data, so it cannot be derived.
Each detected alert carries a deterministic ``dedupe_key`` built from the fact
it describes — *not* from the wall clock:

    scaling_page:page:41:2026-07-29        (page 41, as of that scan date)
    stopped:page:41:2026-07-29
    new_hot_page:page:88                   (once, ever)
    product_surge:product:70:2026-07-31    (as of the day its newest ad landed)

``alerts.dedupe_key`` is UNIQUE, so the sweep is an INSERT OR IGNORE and
re-opening the screen never grows the feed. A row re-appears only when the
underlying fact changes — a new scan, a new jump, new ads on the product. That
is the whole anti-spam mechanism, and it is why this is cheap enough to run
inline.

WHAT WAS DROPPED FROM v1 (deliberately, decision #4 and #6)
-----------------------------------------------------------
* Telegram delivery, its bot token / chat id fields and the enable switch —
  002_parity.sql's header says decision #6 killed Telegram and email, and the
  ``alerts`` table it ships has no delivery state to record it in.
* The admin gate on the settings card ("Only workspace admins can change alert
  settings"). There are no users in v2.
* workspace_id on every query and every table.

WHAT WAS ADDED
--------------
* A fourth alert type, ``stopped`` — the name 002_parity.sql itself gives in
  the ``alert_type`` comment. It is the exact mirror of scaling (same two
  thresholds, applied to a drop instead of a jump), so it needs no new setting,
  and a page that just switched off 60 ads is the single most useful thing this
  screen can tell the owner. Its filter is appended *after* v1's three so every
  v1 control keeps its position.
"""

from __future__ import annotations

import os
import secrets
from typing import Any

from flask import Blueprint, flash, redirect, render_template, request, url_for

from .. import db
from ..time_utils import age_seconds, utc_now, utc_shift, utc_today

bp = Blueprint("alerts", __name__)


@bp.record_once
def _ensure_session_key(state) -> None:
    """Same guard as app/routes/pages.py: flash() needs a signed session."""
    app = state.app
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = (
            os.environ.get("ADSPY2_SECRET_KEY") or secrets.token_hex(32)
        )


# ---------------------------------------------------------------------------
# vocabulary — v1's three types, in v1's order, plus `stopped`
# ---------------------------------------------------------------------------
SCALING_PAGE = "scaling_page"
NEW_HOT_PAGE = "new_hot_page"
PRODUCT_SURGE = "product_surge"
STOPPED = "stopped"

ALERT_TYPES: tuple[str, ...] = (SCALING_PAGE, NEW_HOT_PAGE, PRODUCT_SURGE, STOPPED)

# v1: tabs/alerts/ui.py:TYPE_LABELS.
TYPE_LABELS: dict[str, str] = {
    SCALING_PAGE: "Scaling page",
    NEW_HOT_PAGE: "New hot page",
    PRODUCT_SURGE: "Product surge",
    STOPPED: "Stopped",
}

# v1's toolbar, filter for filter (tabs/alerts/ui.py:renderFilters).
FILTER_ALL = "unread_first"
FILTER_UNREAD = "unread"
FILTERS: tuple[tuple[str, str], ...] = (
    (FILTER_ALL, "All"),
    (FILTER_UNREAD, "Unread"),
    (SCALING_PAGE, "Scaling"),
    (NEW_HOT_PAGE, "New hot"),
    (PRODUCT_SURGE, "Product surge"),
    (STOPPED, "Stopped"),
)

FEED_LIMIT = 200
SWEEP_COOLDOWN_SECONDS = 600          # v1: SWEEP_COOLDOWN_SECONDS
SETTING_PREFIX = "alerts."
LAST_SWEEP_KEY = SETTING_PREFIX + "last_sweep_at"

# v1: services/alerts_service.py:DEFAULT_SETTINGS, minus the Telegram three.
# Order matters — the settings card renders it (v1's order, v1's labels).
SETTING_FIELDS: tuple[tuple[str, str, float, float], ...] = (
    ("scaling_threshold_pct", "Scaling jump %", 1, 10_000),
    ("scaling_threshold_abs", "Scaling jump ads", 1, 1_000_000),
    ("new_hot_page_min_ads", "New hot page min ads", 1, 1_000_000),
    ("new_hot_page_window_days", "New page window (days)", 1, 365),
    ("product_surge_min_ads", "Product surge min ads", 1, 1_000_000),
    ("product_surge_min_advertisers", "Surge min advertisers", 1, 10_000),
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "alerts_enabled": 1,
    "scaling_threshold_pct": 30.0,
    "scaling_threshold_abs": 10,
    "new_hot_page_min_ads": 25,
    "new_hot_page_window_days": 7,
    "product_surge_min_ads": 20,
    "product_surge_min_advertisers": 3,
}

_FLOAT_KEYS = {"scaling_threshold_pct"}


# ---------------------------------------------------------------------------
# settings — 001's k/v `settings` table under the `alerts.` prefix, exactly as
# 002_parity.sql's header says ("one k/v table replaces v1's six singleton
# config tables").
# ---------------------------------------------------------------------------
def get_alert_settings() -> dict[str, Any]:
    stored = {
        str(row["key"])[len(SETTING_PREFIX):]: str(row["value"])
        for row in db.fetch_all(
            "SELECT key, value FROM settings WHERE key LIKE ?", (SETTING_PREFIX + "%",)
        )
    }
    settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
    for key, fallback in DEFAULT_SETTINGS.items():
        raw = stored.get(key)
        if raw is None or raw == "":
            continue
        try:
            settings[key] = float(raw) if key in _FLOAT_KEYS else int(float(raw))
        except (TypeError, ValueError):
            settings[key] = fallback
    settings["last_sweep_at"] = stored.get("last_sweep_at") or None
    return settings


def _clamp(value: Any, *, low: float, high: float, fallback: float) -> float:
    """v1: services/alerts_service.py:_clamp_number."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return min(max(parsed, low), high)


def _write_settings(values: dict[str, Any]) -> None:
    now = utc_now()
    with db.transaction():
        for key, value in values.items():
            db.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (SETTING_PREFIX + key, str(value), now),
            )


def save_alert_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Merge the submitted fields over the stored ones. Absent key = unchanged."""
    current = get_alert_settings()
    changes: dict[str, Any] = {}

    if "alerts_enabled" in payload:
        changes["alerts_enabled"] = 1 if payload.get("alerts_enabled") else 0
    for key, _label, low, high in SETTING_FIELDS:
        if key not in payload:
            continue
        value = _clamp(payload[key], low=low, high=high, fallback=float(current[key]))
        changes[key] = value if key in _FLOAT_KEYS else int(value)

    if changes:
        _write_settings(changes)
    return get_alert_settings()


# ---------------------------------------------------------------------------
# detectors — read-only, nothing is written here
# ---------------------------------------------------------------------------
# The last two daily readings per page. ROW_NUMBER beats a correlated subquery
# here because we need *both* rows and there are only ~2k metric rows in total.
_PAGE_DELTA_SQL = """
    WITH ranked AS (
        SELECT page_id, metric_date, active_ads,
               ROW_NUMBER() OVER (PARTITION BY page_id ORDER BY metric_date DESC) AS rn
        FROM page_daily_metrics
    )
    SELECT p.id,
           p.name,
           p.alias,
           p.platform_page_id,
           p.active_ads          AS current_ads,
           p.prev_active_ads     AS prev_active_ads,
           p.last_delta          AS last_delta,
           p.last_new_ads        AS last_new_ads,
           p.last_stopped_ads    AS last_stopped_ads,
           cur.metric_date       AS as_of,
           prev.active_ads       AS metric_baseline,
           b.active_ads          AS stored_baseline
    FROM v_page p
    LEFT JOIN ranked cur  ON cur.page_id  = p.id AND cur.rn  = 1
    LEFT JOIN ranked prev ON prev.page_id = p.id AND prev.rn = 2
    LEFT JOIN alert_page_baselines b ON b.page_id = p.id
    WHERE p.is_hidden = 0 AND p.is_removed = 0
"""

_NEW_HOT_SQL = """
    SELECT p.id, p.name, p.alias, p.platform_page_id,
           p.active_ads AS current_ads,
           COALESCE(p.first_captured_at, p.created_at) AS first_captured_at
    FROM v_page p
    WHERE p.is_hidden = 0 AND p.is_removed = 0
      AND p.active_ads >= ?
      AND COALESCE(p.first_captured_at, p.created_at) >= ?
    ORDER BY p.active_ads DESC
"""

# v1 counted DISTINCT advertiser_page_id; v2's ads carry page_id directly.
_PRODUCT_SURGE_SQL = """
    SELECT pr.id,
           COALESCE(NULLIF(pr.display_name, ''), pr.normalized_name) AS product_name,
           COUNT(DISTINCT a.id)      AS active_ads,
           COUNT(DISTINCT a.page_id) AS advertiser_count,
           MAX(a.first_captured_at)  AS newest_ad_at
    FROM v_product pr
    JOIN ad_products ap ON ap.product_id = pr.id
    JOIN ads a          ON a.id = ap.ad_id AND a.status = 'active'
    LEFT JOIN product_removed rm ON rm.normalized_name = pr.normalized_name
    WHERE rm.normalized_name IS NULL AND pr.is_hidden = 0
    GROUP BY pr.id
    HAVING COUNT(DISTINCT a.id) >= ? AND COUNT(DISTINCT a.page_id) >= ?
    ORDER BY active_ads DESC
"""


def _page_name(row: Any) -> str:
    """v1: _page_display_name — alias wins, then name, then the platform id."""
    return str(
        (row["alias"] if "alias" in row.keys() else None)
        or row["name"]
        or row["platform_page_id"]
        or f"page #{row['id']}"
    )


def _baseline_for(row: Any) -> int | None:
    """The fixed point a jump is measured from.

    Preference order, and each fallback exists for a real case:
      1. the previous daily reading  — the true "since the last scan" number;
      2. alert_page_baselines        — set the first time the sweep saw this
                                       page, so a page with one reading still
                                       has something to compare against;
      3. pages.prev_active_ads       — ingest's own bookkeeping;
      4. active_ads - last_delta     — the same fact from the other direction,
                                       for a page whose metrics were pruned.
    """
    for key in ("metric_baseline", "stored_baseline", "prev_active_ads"):
        value = row[key]
        if value is not None and int(value) > 0:
            return int(value)
    delta = int(row["last_delta"] or 0)
    if delta:
        implied = int(row["current_ads"] or 0) - delta
        if implied > 0:
            return implied
    return None


def detect_page_moves(settings: dict[str, Any]) -> tuple[list[dict], list[tuple[int, int]]]:
    """Scaling and stopped alerts, plus the baselines the sweep should record."""
    threshold_pct = float(settings["scaling_threshold_pct"])
    threshold_abs = int(settings["scaling_threshold_abs"])
    today = utc_today()

    alerts: list[dict] = []
    baselines: list[tuple[int, int]] = []
    for row in db.fetch_all(_PAGE_DELTA_SQL):
        page_id = int(row["id"])
        current = int(row["current_ads"] or 0)
        baseline = _baseline_for(row)
        if baseline is None:
            # First sighting: record where the page is now so the next sweep
            # has something to measure against (v1 does exactly this).
            baselines.append((page_id, current))
            continue

        as_of = str(row["as_of"] or today)
        move = current - baseline
        pct = abs(move) / baseline * 100.0
        if abs(move) < threshold_abs or pct < threshold_pct:
            continue

        name = _page_name(row)
        if move > 0:
            alerts.append({
                "entity_type": "page",
                "entity_id": page_id,
                "alert_type": SCALING_PAGE,
                "severity": "high" if (pct >= 100 or move >= 50) else "warn",
                "current_value": str(current),
                "previous_value": str(baseline),
                "dedupe_key": f"{SCALING_PAGE}:page:{page_id}:{as_of}",
                "message": (
                    f"Scaling page: {name} jumped from {baseline} to {current} "
                    f"active ads (+{move}, {pct:.0f}%)."
                ),
            })
        else:
            stopped = int(row["last_stopped_ads"] or 0)
            tail = f" {stopped} ads stopped in the last scan." if stopped else ""
            alerts.append({
                "entity_type": "page",
                "entity_id": page_id,
                "alert_type": STOPPED,
                "severity": "high" if (pct >= 100 or -move >= 50) else "warn",
                "current_value": str(current),
                "previous_value": str(baseline),
                "dedupe_key": f"{STOPPED}:page:{page_id}:{as_of}",
                "message": (
                    f"Stopped: {name} dropped from {baseline} to {current} "
                    f"active ads ({move}, {pct:.0f}%).{tail}"
                ),
            })
        baselines.append((page_id, current))
    return alerts, baselines


def detect_new_hot_pages(settings: dict[str, Any]) -> list[dict]:
    min_ads = int(settings["new_hot_page_min_ads"])
    window_days = int(settings["new_hot_page_window_days"])
    cutoff = utc_shift(-window_days * 86400)
    rows = db.fetch_all(_NEW_HOT_SQL, (min_ads, cutoff))
    alerts = []
    for row in rows:
        page_id = int(row["id"])
        current = int(row["current_ads"] or 0)
        alerts.append({
            "entity_type": "page",
            "entity_id": page_id,
            "alert_type": NEW_HOT_PAGE,
            "severity": "info",
            "current_value": str(current),
            "previous_value": None,
            # Once per page, ever — a page is only new once.
            "dedupe_key": f"{NEW_HOT_PAGE}:page:{page_id}",
            "message": (
                f"New hot page: {_page_name(row)} first seen within "
                f"{window_days} days and already running {current} active ads."
            ),
        })
    return alerts


def detect_product_surges(settings: dict[str, Any]) -> list[dict]:
    min_ads = int(settings["product_surge_min_ads"])
    min_advertisers = int(settings["product_surge_min_advertisers"])
    alerts = []
    for row in db.fetch_all(_PRODUCT_SURGE_SQL, (min_ads, min_advertisers)):
        product_id = int(row["id"])
        active_ads = int(row["active_ads"] or 0)
        advertisers = int(row["advertiser_count"] or 0)
        # "As of" the day the product's newest live ad landed: the surge is
        # re-announced when it grows, not once a day for ever.
        as_of = str(row["newest_ad_at"] or utc_today())[:10]
        alerts.append({
            "entity_type": "product",
            "entity_id": product_id,
            "alert_type": PRODUCT_SURGE,
            "severity": "high" if advertisers >= min_advertisers * 3 else "warn",
            "current_value": str(active_ads),
            "previous_value": str(advertisers),
            "dedupe_key": f"{PRODUCT_SURGE}:product:{product_id}:{as_of}",
            "message": (
                f"Product surge: '{row['product_name']}' has {active_ads} "
                f"active ads across {advertisers} advertisers."
            ),
        })
    return alerts


def evaluate_alerts(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Every alert the current data implies. Read-only."""
    settings = settings or get_alert_settings()
    moves, baselines = detect_page_moves(settings)
    return {
        "alerts": moves + detect_new_hot_pages(settings) + detect_product_surges(settings),
        "baselines": baselines,
    }


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------
def run_alert_sweep(*, force: bool = False) -> dict[str, Any]:
    """Evaluate and persist. Called on GET /alerts and by "Run sweep now".

    INSERT OR IGNORE against the UNIQUE dedupe_key does the de-duplication, so
    this is idempotent: running it twice in a row creates nothing the second
    time.
    """
    settings = get_alert_settings()
    if not int(settings.get("alerts_enabled") or 0):
        return {"created": 0, "skipped": True, "reason": "alerts_disabled"}

    if not force:
        elapsed = age_seconds(settings.get("last_sweep_at"))
        if elapsed is not None and elapsed < SWEEP_COOLDOWN_SECONDS:
            return {"created": 0, "skipped": True, "reason": "cooldown"}

    result = evaluate_alerts(settings)
    now = utc_now()
    created = 0
    with db.transaction():
        for alert in result["alerts"]:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO alerts (
                    entity_type, entity_id, alert_type, severity, message,
                    current_value, previous_value, dedupe_key, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert["entity_type"],
                    int(alert["entity_id"]),
                    alert["alert_type"],
                    alert["severity"],
                    alert["message"],
                    alert.get("current_value"),
                    alert.get("previous_value"),
                    alert["dedupe_key"],
                    now,
                ),
            )
            created += int(cursor.rowcount or 0)
        for page_id, active_ads in result["baselines"]:
            db.execute(
                """
                INSERT INTO alert_page_baselines (page_id, active_ads, recorded_at)
                VALUES (?, ?, ?)
                ON CONFLICT(page_id) DO UPDATE SET
                    active_ads  = excluded.active_ads,
                    recorded_at = excluded.recorded_at
                """,
                (int(page_id), int(active_ads), now),
            )
        db.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (LAST_SWEEP_KEY, now, now),
        )
    return {"created": created, "skipped": False, "reason": ""}


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------
def time_ago(value: str | None) -> str:
    """v1's timeAgo(), server-side. '3h ago', '2d ago', 'just now'."""
    seconds = age_seconds(value)
    if seconds is None:
        return str(value or "")
    minutes = max(0, int(round(seconds / 60)))
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = int(round(minutes / 60))
    if hours < 48:
        return f"{hours}h ago"
    return f"{int(round(hours / 24))}d ago"


def _decorate(row: Any) -> dict:
    alert = dict(row)
    alert["type_label"] = TYPE_LABELS.get(alert["alert_type"], alert["alert_type"])
    alert["is_unread"] = alert.get("read_at") is None
    alert["when"] = time_ago(alert.get("generated_at"))
    alert["entity_name"] = alert.get("entity_name") or ""
    return alert


_FEED_SQL = """
    SELECT a.id, a.entity_type, a.entity_id, a.alert_type, a.severity,
           a.message, a.current_value, a.previous_value,
           a.generated_at, a.read_at, a.resolved_at,
           CASE WHEN a.entity_type = 'page'
                THEN COALESCE(NULLIF(p.alias, ''), NULLIF(p.name, ''), p.platform_page_id)
                WHEN a.entity_type = 'product'
                THEN COALESCE(NULLIF(pr.display_name, ''), pr.normalized_name)
           END AS entity_name
    FROM alerts a
    LEFT JOIN pages    p  ON a.entity_type = 'page'    AND p.id  = a.entity_id
    LEFT JOIN products pr ON a.entity_type = 'product' AND pr.id = a.entity_id
"""


def load_alerts(*, alert_filter: str = FILTER_ALL, limit: int = FEED_LIMIT) -> list[dict]:
    sql = _FEED_SQL
    params: list[Any] = []
    if alert_filter == FILTER_UNREAD:
        sql += " WHERE a.read_at IS NULL"
    elif alert_filter in ALERT_TYPES:
        sql += " WHERE a.alert_type = ?"
        params.append(alert_filter)
    # v1's default filter is called "unread_first" and means exactly that.
    # The type/severity tie-break matters: one sweep stamps every row it
    # creates with the same generated_at, and without it a 53-row
    # product-surge batch buries the two pages that actually doubled
    # overnight. It only ever reorders rows born in the same sweep — across
    # sweeps generated_at still wins, exactly as in v1.
    sql += (
        " ORDER BY (a.read_at IS NULL) DESC, a.generated_at DESC,"
        " CASE a.alert_type WHEN ? THEN 0 WHEN ? THEN 1 WHEN ? THEN 2 ELSE 3 END,"
        " CASE a.severity WHEN 'high' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,"
        " a.id DESC LIMIT ?"
    )
    params.extend([SCALING_PAGE, STOPPED, NEW_HOT_PAGE])
    params.append(max(1, min(int(limit), 1000)))
    return [_decorate(row) for row in db.fetch_all(sql, params)]


def alert_counts() -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN read_at IS NULL THEN 1 ELSE 0 END) AS unread FROM alerts"
    )
    by_type = {
        str(item["alert_type"]): int(item["amount"] or 0)
        for item in db.fetch_all(
            "SELECT alert_type, COUNT(*) AS amount FROM alerts GROUP BY alert_type"
        )
    }
    return {
        "total": int((row["total"] if row else 0) or 0),
        "unread": int((row["unread"] if row else 0) or 0),
        "by_type": {name: by_type.get(name, 0) for name in ALERT_TYPES},
    }


def _filter_counts(counts: dict[str, Any]) -> dict[str, int]:
    by_type = counts["by_type"]
    return {
        FILTER_ALL: counts["total"],
        FILTER_UNREAD: counts["unread"],
        **{name: by_type.get(name, 0) for name in ALERT_TYPES},
    }


# ---------------------------------------------------------------------------
# routes — plain form POST + redirect, like every other v2 screen
# ---------------------------------------------------------------------------
def _requested_filter() -> str:
    wanted = str(request.args.get("filter") or FILTER_ALL).strip()
    valid = {key for key, _label in FILTERS}
    return wanted if wanted in valid else FILTER_ALL


def _back() -> str:
    return request.form.get("next") or url_for("alerts.index")


@bp.get("/alerts")
def index():
    outcome = run_alert_sweep()
    alert_filter = _requested_filter()
    counts = alert_counts()
    settings = get_alert_settings()
    return render_template(
        "alerts.html",
        title="Scaling Alerts",
        active_nav="alerts",
        alerts=load_alerts(alert_filter=alert_filter),
        counts=counts,
        filters=FILTERS,
        filter_counts=_filter_counts(counts),
        current_filter=alert_filter,
        settings=settings,
        setting_fields=SETTING_FIELDS,
        last_sweep=time_ago(settings.get("last_sweep_at")) if settings.get("last_sweep_at") else "never",
        sweep_skipped=outcome.get("reason") or "",
        cooldown_minutes=SWEEP_COOLDOWN_SECONDS // 60,
    )


@bp.post("/alerts/<int:alert_id>/read")
def mark_read(alert_id: int):
    with db.transaction():
        cursor = db.execute(
            "UPDATE alerts SET read_at = ? WHERE id = ? AND read_at IS NULL",
            (utc_now(), alert_id),
        )
    if not cursor.rowcount:
        flash("That alert was already read.", "warning")
    return redirect(_back())


@bp.post("/alerts/read-all")
def mark_all_read():
    with db.transaction():
        cursor = db.execute(
            "UPDATE alerts SET read_at = ? WHERE read_at IS NULL", (utc_now(),)
        )
    updated = int(cursor.rowcount or 0)
    flash(
        f"{updated} alert{'' if updated == 1 else 's'} marked read."
        if updated else "Nothing unread.",
        "success" if updated else "warning",
    )
    return redirect(_back())


@bp.post("/alerts/sweep")
def sweep_now():
    result = run_alert_sweep(force=True)
    if result.get("reason") == "alerts_disabled":
        flash("Alerts are switched off — turn them on in Alert settings.", "warning")
    elif result["created"]:
        flash(f"{result['created']} new alert(s) found.", "success")
    else:
        flash("Sweep complete. No new alerts.", "success")
    return redirect(_back())


@bp.post("/alerts/settings")
def update_settings():
    payload: dict[str, Any] = {"alerts_enabled": bool(request.form.get("alerts_enabled"))}
    for key, _label, _low, _high in SETTING_FIELDS:
        if request.form.get(key, "").strip() != "":
            payload[key] = request.form.get(key)
    save_alert_settings(payload)
    flash("Alert settings saved.", "success")
    return redirect(_back())


__all__ = [
    "bp",
    "ALERT_TYPES",
    "TYPE_LABELS",
    "DEFAULT_SETTINGS",
    "get_alert_settings",
    "save_alert_settings",
    "evaluate_alerts",
    "run_alert_sweep",
    "load_alerts",
    "alert_counts",
    "time_ago",
]
