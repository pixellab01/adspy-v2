"""Overview — v1's landing screen, rebuilt on v2's tables.

WHAT THIS IS
v1 serves this at ``/overview`` from ``tabs/overview/{actions,ui,queries}.py``
(a 122 KB f-string UI on top of a 102 KB query module). The screen is, top to
bottom:

    toolbar        All / Favorites / Scaling / Names(count)   ...  "N pages shown"
    kpi-grid       Pages Tracked | Active Unique Ads | Represented Ads | Scaling Alerts
    pin-strip      "Pinned Pages" — the favourited pages, as cards
    panel          "Top Performing Pages"   [Live] [Rebuild] [Export CSV]
                   rank | Page | Live Ads | Top Product | Meta Results | Oldest Ad | 7d Trend
    row-2          "Top Products by Ad Volume"  +  "Recent Research"
    names panel    "Page IDs needing names"

Every one of those is reproduced here, in that order, with those labels.

WHAT IS DIFFERENT, AND WHY
* **Nothing is cached.** v1 reads ``overview_current_kpis`` / ``overview_top_pages``
  / ``overview_top_products`` — read models that a completed scrape invalidates,
  which is why v1's own KPI code stopped trusting them and recomputes live. v2
  has no cache tables at all (docs/00-decisions.md), so every number below is
  counted from ``pages`` / ``ads`` / ``page_daily_metrics`` at request time.
  394 pages and 17.5k ads cost single-digit milliseconds.
* **The four filters are links, not JavaScript.** v1 hides rows client-side;
  here ``?view=`` re-queries, so the screen works with JS off and a filtered
  view is a URL you can keep.
* **"Names" renames by hand.** v1's queue calls Gemini to invent a page name
  (three models, rotating API keys). v2 has no LLM and none is coming, so the
  row carries an input that POSTs to the alias endpoint the Page Analyzer
  already owns — same screen, same place, same outcome, minus the robot.

The filter/rank/limit constants are v1's: OVERVIEW_TOP_PAGE_LIMIT = 50,
eight products, eight research runs, a 7-day trend window and v1's 20 %
seven-day-growth threshold for "scaling".
"""

from __future__ import annotations

import csv
import io
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable, Sequence

from flask import (
    Blueprint,
    Response,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import db, queries
from ..meta_links import meta_ads_library_url
from ..time_utils import utc_now

bp = Blueprint("overview", __name__)

# --- v1's constants, verbatim ------------------------------------------------
TOP_PAGE_LIMIT = 50            # tabs/overview/queries.py:29 OVERVIEW_TOP_PAGE_LIMIT
TOP_PRODUCT_LIMIT = 8          # ... load_overview(): "LIMIT 8"
RECENT_RESEARCH_LIMIT = 8
UNRESOLVED_LIMIT = 40          # v1 pages the alias queue; so do we
TREND_DAYS = 7
SCALING_GROWTH_THRESHOLD = 20.0  # services/metrics.py:18

VIEWS = ("all", "favorites", "scaling", "names")

# services/../tabs/overview/queries.py:363 — names that carry no identity.
WEAK_PAGE_LABELS = (
    "unknown",
    "unknown page",
    "facebook page",
    "meta page",
    "advertiser",
    "untitled",
    "n a",
    "na",
    "this ad has multiple versions",
    "sponsored",
    "active",
    "see ad details",
)


@bp.record_once
def _ensure_session_key(state) -> None:
    """Same guard as pages.py / queue.py: flash() needs a signed session."""
    app = state.app
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = (
            os.environ.get("ADSPY2_SECRET_KEY") or secrets.token_hex(32)
        )


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _rows(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in db.fetch_all(sql, params)]


def _row(sql: str, params: Iterable[Any] = ()) -> dict:
    found = db.fetch_one(sql, params)
    return dict(found) if found is not None else {}


def _placeholders(values: Sequence[Any]) -> str:
    return ",".join("?" for _ in values)


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _iso_days(days: int = TREND_DAYS) -> list[str]:
    """The last ``days`` dates, oldest first, ending today (UTC).

    UTC because ``page_daily_metrics.metric_date`` is written in UTC and
    SQLite's ``date('now')`` is UTC — mixing in local time would drop or
    duplicate the newest point for anyone east of Greenwich.
    """
    today = datetime.now(UTC).date()
    return [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]


def direction_of(value: float | int | None) -> str:
    """'up' | 'down' | 'flat' — the class suffix every delta in v1 uses."""
    amount = float(value or 0)
    return "up" if amount > 0 else ("down" if amount < 0 else "flat")


# ---------------------------------------------------------------------------
# the live ad rollup, shared by every query below
#
# Counted from `ads`, never from pages.active_ads: that column is ingest's
# bookkeeping and ratchets upward, so a page whose ads all stopped would still
# read high. v1 learned this the hard way (services/metrics.py:62).
# ---------------------------------------------------------------------------
_AD_ROLLUP = """
    LEFT JOIN (
        SELECT page_id,
               SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS live_active,
               SUM(CASE WHEN status = 'active'
                        THEN COALESCE(NULLIF(represented_ad_count, 0), 1)
                        ELSE 0 END)                               AS live_represented,
               MIN(CASE WHEN status = 'active'
                        THEN NULLIF(start_date, '') END)          AS oldest_active_ad_date,
               COUNT(*)                                           AS stored_ads
        FROM ads
        GROUP BY page_id
    ) r ON r.page_id = p.id
"""


# ---------------------------------------------------------------------------
# 7-day trend, one query for every page
# ---------------------------------------------------------------------------
def page_trends(days: int = TREND_DAYS) -> dict[int, dict]:
    """``{page_id: {values, growth, today_delta, has_history}}``.

    v1's ``_load_page_series`` semantics, kept because the sparkline and the
    "scaling" badge have to agree: the series is carried forward across gaps,
    and the baseline is the last snapshot *before* the window when there is
    one, so a page with sparse snapshots starts at the value it really had
    seven days ago rather than at zero.

    ``page_daily_metrics`` is 2,048 rows on the owner's database, so loading
    every page in one pass is cheaper than the per-page queries v1 ran.
    """
    labels = _iso_days(days)
    window = f"-{days - 1} days"

    inside = _rows(
        """
        SELECT page_id, metric_date, COALESCE(active_ads, 0) AS value
        FROM page_daily_metrics
        WHERE date(metric_date) >= date('now', ?)
        ORDER BY page_id, date(metric_date)
        """,
        (window,),
    )
    baselines = _rows(
        """
        SELECT pm.page_id, COALESCE(pm.active_ads, 0) AS value
        FROM page_daily_metrics pm
        WHERE date(pm.metric_date) < date('now', ?)
          AND pm.metric_date = (
              SELECT MAX(x.metric_date) FROM page_daily_metrics x
              WHERE x.page_id = pm.page_id AND date(x.metric_date) < date('now', ?)
          )
        """,
        (window, window),
    )

    by_page: dict[int, dict[str, float]] = {}
    for row in inside:
        page_id = _int(row["page_id"])
        day = str(row["metric_date"] or "")[:10]
        if page_id and day:
            by_page.setdefault(page_id, {})[day] = float(row["value"] or 0)
    baseline_by_page = {_int(r["page_id"]): float(r["value"] or 0) for r in baselines}

    trends: dict[int, dict] = {}
    for page_id in set(by_page) | set(baseline_by_page):
        day_map = by_page.get(page_id, {})
        carried = baseline_by_page.get(page_id)
        if carried is None:
            known = [day_map[label] for label in labels if label in day_map]
            carried = known[0] if known else 0.0
        values: list[float] = []
        for label in labels:
            if label in day_map:
                carried = day_map[label]
            values.append(carried)

        first, last = values[0], values[-1]
        if first > 0:
            growth = round(((last - first) * 100.0) / first, 2)
        else:
            growth = 100.0 if last > 0 else 0.0
        trends[page_id] = {
            "values": [int(v) if float(v).is_integer() else round(v, 2) for v in values],
            "growth": growth,
            "today_delta": round(values[-1] - values[-2], 2) if len(values) > 1 else 0,
            "has_history": True,
        }
    return trends


def _flat_trend(current: int) -> dict:
    return {
        "values": [current] * TREND_DAYS,
        "growth": 0.0,
        "today_delta": 0,
        "has_history": False,
    }


def decorate_trend(row: dict, trends: dict[int, dict]) -> dict:
    """Attach the sparkline geometry and the scaling flag to one page row."""
    current = _int(row.get("active_ads"))
    trend = dict(trends.get(_int(row.get("page_id") or row.get("id")), _flat_trend(current)))
    if not any(trend["values"]) and current:
        trend["values"] = [current] * TREND_DAYS

    width, height = 72, 26
    row["trend_values"] = trend["values"]
    row["trend_points"] = queries.sparkline_points(trend["values"], width=width, height=height)
    row["trend_up"] = trend["values"][-1] >= trend["values"][0]
    row["trend_end_y"] = _last_y(row["trend_points"], height)
    row["today_delta"] = trend["today_delta"]
    row["seven_day_growth"] = trend["growth"]
    row["has_trend_history"] = trend["has_history"]
    row["is_scaling"] = bool(trend["growth"] >= SCALING_GROWTH_THRESHOLD)
    return row


def _last_y(points: str, height: int) -> float:
    """The y of the final sparkline point, for the dot v1 draws at the end."""
    if not points:
        return height / 2
    try:
        return float(points.rsplit(",", 1)[1])
    except (IndexError, ValueError):  # pragma: no cover - defensive
        return height / 2


# ---------------------------------------------------------------------------
# KPI tiles
# ---------------------------------------------------------------------------
def scan_change_percent() -> float:
    """Percent change in active ads between the two most recent snapshots.

    v1's rule (services/metrics.py:272): prefer ``page_scan_history``, which is
    the precise per-scan record, and fall back to ``page_daily_metrics`` when no
    page has two scan snapshots yet — which is the case on the imported
    database, where page_scan_history is empty. Only pages that appear on both
    sides count, or a page first seen today would read as infinite growth.
    """
    for sql in (
        """
        WITH ranked AS (
            SELECT h.page_id, COALESCE(h.active_unique_ads, 0) AS value,
                   ROW_NUMBER() OVER (PARTITION BY h.page_id
                                      ORDER BY h.captured_at DESC, h.id DESC) AS rn
            FROM page_scan_history h
            JOIN pages p ON p.id = h.page_id AND p.is_hidden = 0
        )
        SELECT COALESCE(SUM(CASE WHEN rn = 1 THEN value END), 0) AS current_ads,
               COALESCE(SUM(CASE WHEN rn = 2 THEN value END), 0) AS previous_ads,
               COUNT(DISTINCT CASE WHEN rn = 2 THEN page_id END) AS comparable
        FROM ranked
        WHERE rn <= 2 AND page_id IN (SELECT page_id FROM ranked WHERE rn = 2)
        """,
        """
        WITH ranked AS (
            SELECT pm.page_id, COALESCE(pm.active_ads, 0) AS value,
                   ROW_NUMBER() OVER (PARTITION BY pm.page_id
                                      ORDER BY pm.metric_date DESC) AS rn
            FROM page_daily_metrics pm
            JOIN pages p ON p.id = pm.page_id AND p.is_hidden = 0
        )
        SELECT COALESCE(SUM(CASE WHEN rn = 1 THEN value END), 0) AS current_ads,
               COALESCE(SUM(CASE WHEN rn = 2 THEN value END), 0) AS previous_ads,
               COUNT(DISTINCT CASE WHEN rn = 2 THEN page_id END) AS comparable
        FROM ranked
        WHERE rn <= 2 AND page_id IN (SELECT page_id FROM ranked WHERE rn = 2)
        """,
    ):
        row = _row(sql)
        comparable = _int(row.get("comparable"))
        previous = _int(row.get("previous_ads"))
        if comparable > 0 and previous > 0:
            return round((_int(row.get("current_ads")) - previous) * 100.0 / previous, 2)
    return 0.0


def kpis(trends: dict[int, dict]) -> dict:
    """The four tiles. Every number is counted here, nothing is read back."""
    pages = _row(
        """
        SELECT COUNT(*) AS pages_tracked,
               COALESCE(SUM(CASE
                   WHEN date(COALESCE(p.first_captured_at, p.created_at)) >= date('now', '-7 day')
                   THEN 1 ELSE 0 END), 0) AS new_pages_week
        FROM pages p
        WHERE p.is_hidden = 0
        """
    )
    ads = _row(
        """
        SELECT COUNT(*) AS active_unique_ads,
               COALESCE(SUM(COALESCE(NULLIF(a.represented_ad_count, 0), 1)), 0) AS represented_ads
        FROM ads a
        JOIN pages p ON p.id = a.page_id AND p.is_hidden = 0
        WHERE a.status = 'active'
        """
    )
    # v1 labels this "creative families" and never populates it, so it hides the
    # sub-line. v2 does have the thing v1 meant: script_clusters, one row per
    # group of near-identical ad scripts. Real number, so it is shown.
    families = _int(_row("SELECT COUNT(*) AS n FROM script_clusters").get("n"))
    groups = _int(_row('SELECT COUNT(*) AS n FROM "groups"').get("n"))

    # Explicit unresolved alert rows win when the alert sweep has produced any
    # (v1: services/metrics.py:388); otherwise count pages growing >= 20 % over
    # seven days, which is the same rule the per-row badge uses.
    scaling = _int(
        _row(
            """
            SELECT COUNT(*) AS n FROM alerts
            WHERE alert_type = 'scaling' AND resolved_at IS NULL
            """
        ).get("n")
    )
    if scaling <= 0:
        visible = {
            _int(r["id"])
            for r in _rows("SELECT id FROM pages WHERE is_hidden = 0")
        }
        scaling = sum(
            1
            for page_id, trend in trends.items()
            if page_id in visible and trend["growth"] >= SCALING_GROWTH_THRESHOLD
        )

    change = scan_change_percent()
    return {
        "pages_tracked": _int(pages.get("pages_tracked")),
        "new_pages_week": _int(pages.get("new_pages_week")),
        "active_unique_ads": _int(ads.get("active_unique_ads")),
        "represented_ads": _int(ads.get("represented_ads")),
        "creative_families": families,
        "creative_families_available": families > 0,
        "brand_groups": groups,
        "scaling_alerts": scaling,
        "change_since_previous_scan": change,
        "change_direction": direction_of(change),
        "new_pages_direction": direction_of(_int(pages.get("new_pages_week"))),
    }


# ---------------------------------------------------------------------------
# Top Performing Pages
# ---------------------------------------------------------------------------
_PAGE_SELECT = f"""
SELECT p.id                                   AS page_id,
       p.platform_page_id,
       p.name,
       p.alias,
       p.url,
       p.fb_estimated_results,
       p.last_verified_at,
       p.last_captured_at,
       COALESCE(r.live_active, 0)             AS active_ads,
       COALESCE(r.live_represented, 0)        AS represented_ads,
       COALESCE(r.stored_ads, 0)              AS stored_ads,
       r.oldest_active_ad_date                AS oldest_active_ad_date,
       COALESCE(s.is_favorite, 0)             AS is_favorite,
       (SELECT g.name FROM group_pages gp
          JOIN "groups" g ON g.id = gp.group_id
         WHERE gp.page_id = p.id
         ORDER BY g.name LIMIT 1)             AS group_name
FROM pages p
{_AD_ROLLUP}
LEFT JOIN page_states s ON s.page_id = p.id
WHERE p.is_hidden = 0
ORDER BY active_ads DESC, represented_ads DESC, p.id
"""


def _decorate_page(row: dict) -> dict:
    name = (row.get("alias") or "").strip() or (row.get("name") or "").strip()
    row["display_name"] = name or f"Page {row.get('platform_page_id') or row['page_id']}"
    row["has_alias"] = bool((row.get("alias") or "").strip())
    row["library_url"] = meta_ads_library_url(row.get("platform_page_id"), row.get("url"))
    row["oldest_days"] = _days_since(row.get("oldest_active_ad_date"))
    row["is_favorite"] = bool(row.get("is_favorite"))
    estimate = row.get("fb_estimated_results")
    row["meta_results_text"] = f"{_int(estimate):,}" if estimate else "-"
    return row


def _days_since(day: Any) -> int | None:
    """Whole days since a ``YYYY-MM-DD`` ad start date; None when unknown."""
    text = str(day or "")[:10]
    if not text:
        return None
    try:
        started = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None
    return max(0, (datetime.now(UTC).date() - started).days)


def ranked_pages(trends: dict[int, dict]) -> list[dict]:
    """Every visible page, ranked exactly the way v1 ranks the table.

    Ranking is done over the whole set before any filter is applied, so
    "Favorites" shows a page's real position (rank 07) rather than renumbering
    it to 01 — which is what the number is for.
    """
    rows = [_decorate_page(row) for row in _rows(_PAGE_SELECT)]
    for position, row in enumerate(rows, start=1):
        row["rank_position"] = position
        decorate_trend(row, trends)
    return rows


def attach_top_products(rows: list[dict]) -> None:
    """One query for the whole visible slice — v1 ran this per page, 200 times.

    An ad can carry several product links; the highest-confidence one wins and
    ties break on the lowest product id, so the label is deterministic.
    """
    ids = [_int(row["page_id"]) for row in rows if row.get("page_id")]
    if not ids:
        return
    found = {
        _int(row["page_id"]): row
        for row in _rows(
            f"""
            WITH chosen AS (
                SELECT ap.ad_id, ap.product_id,
                       ROW_NUMBER() OVER (PARTITION BY ap.ad_id
                                          ORDER BY ap.confidence DESC, ap.product_id) AS link_rank
                FROM ad_products ap
            ),
            per_page AS (
                SELECT a.page_id                                             AS page_id,
                       pr.id                                                 AS product_id,
                       COALESCE(NULLIF(TRIM(pr.display_name), ''),
                                pr.normalized_name)                          AS product_name,
                       COUNT(DISTINCT a.id)                                  AS ads
                FROM chosen c
                JOIN ads a      ON a.id = c.ad_id AND a.status = 'active'
                JOIN products pr ON pr.id = c.product_id
                WHERE c.link_rank = 1 AND a.page_id IN ({_placeholders(ids)})
                GROUP BY a.page_id, pr.id
            )
            SELECT page_id, product_id, product_name FROM (
                SELECT per_page.*,
                       ROW_NUMBER() OVER (PARTITION BY page_id
                                          ORDER BY ads DESC, product_id) AS product_rank
                FROM per_page
            ) WHERE product_rank = 1
            """,
            ids,
        )
    }
    for row in rows:
        hit = found.get(_int(row["page_id"]))
        row["top_product"] = (hit or {}).get("product_name") or ""
        row["top_product_id"] = _int((hit or {}).get("product_id"))


# ---------------------------------------------------------------------------
# Pinned Pages
# ---------------------------------------------------------------------------
def pinned_pages(ranked: list[dict]) -> list[dict]:
    return [row for row in ranked if row["is_favorite"]]


# ---------------------------------------------------------------------------
# Top Products by Ad Volume
# ---------------------------------------------------------------------------
def top_products(limit: int = TOP_PRODUCT_LIMIT) -> list[dict]:
    rows = _rows(
        """
        WITH chosen AS (
            SELECT ap.ad_id, ap.product_id,
                   ROW_NUMBER() OVER (PARTITION BY ap.ad_id
                                      ORDER BY ap.confidence DESC, ap.product_id) AS link_rank
            FROM ad_products ap
        )
        SELECT pr.id                                                     AS product_id,
               COALESCE(NULLIF(TRIM(pr.display_name), ''),
                        pr.normalized_name)                              AS product_name,
               COALESCE(pr.domain, '')                                   AS store_domain,
               COUNT(DISTINCT CASE WHEN a.status = 'active' THEN a.id END) AS active_unique_ads,
               COALESCE(SUM(CASE WHEN a.status = 'active'
                                 THEN COALESCE(NULLIF(a.represented_ad_count, 0), 1)
                                 ELSE 0 END), 0)                         AS represented_ads,
               COUNT(DISTINCT a.page_id)                                 AS advertiser_count
        FROM chosen c
        JOIN ads a       ON a.id = c.ad_id
        JOIN products pr ON pr.id = c.product_id
        JOIN pages p     ON p.id = a.page_id AND p.is_hidden = 0
        WHERE c.link_rank = 1
        GROUP BY pr.id
        HAVING active_unique_ads > 0
        ORDER BY active_unique_ads DESC, represented_ads DESC, pr.id
        LIMIT ?
        """,
        (int(limit),),
    )
    peak = max((_int(row["active_unique_ads"]) for row in rows), default=1) or 1
    for position, row in enumerate(rows, start=1):
        row["rank_position"] = position
        # v1's floor of 8 %: a product with two ads still has a visible bar.
        row["bar_width"] = max(8.0, round(_int(row["active_unique_ads"]) * 100.0 / peak, 1))
    return rows


# ---------------------------------------------------------------------------
# Recent Research
# ---------------------------------------------------------------------------
def recent_research(limit: int = RECENT_RESEARCH_LIMIT) -> list[dict]:
    return _rows(
        """
        SELECT kr.id, kq.keyword, kq.country, kr.status, kr.requested_at,
               kr.finished_at, kr.unique_pages, kr.ads_scanned
        FROM keyword_runs kr
        JOIN keyword_queries kq ON kq.id = kr.query_id
        ORDER BY kr.id DESC
        LIMIT ?
        """,
        (int(limit),),
    )


# ---------------------------------------------------------------------------
# "Names" — pages whose identity is a number
# ---------------------------------------------------------------------------
# v1's SQL guard (tabs/overview/queries.py:429), translated to v2's columns.
# A page with an alias is resolved by definition, so it never appears here.
_UNRESOLVED_WHERE = f"""
    p.is_hidden = 0
    AND TRIM(COALESCE(p.alias, '')) = ''
    AND (
        TRIM(COALESCE(p.name, '')) = ''
        OR lower(TRIM(COALESCE(p.name, ''))) IN ({_placeholders(WEAK_PAGE_LABELS)})
        OR (TRIM(COALESCE(p.platform_page_id, '')) <> ''
            AND lower(TRIM(COALESCE(p.name, ''))) = lower(TRIM(COALESCE(p.platform_page_id, ''))))
        OR (TRIM(COALESCE(p.name, '')) <> ''
            AND replace(TRIM(COALESCE(p.name, '')), ' ', '') NOT GLOB '*[^0-9]*')
        OR (TRIM(COALESCE(p.platform_page_id, '')) <> ''
            AND lower(TRIM(COALESCE(p.name, ''))) IN (
                'page '          || lower(TRIM(COALESCE(p.platform_page_id, ''))),
                'meta page '     || lower(TRIM(COALESCE(p.platform_page_id, ''))),
                'facebook page ' || lower(TRIM(COALESCE(p.platform_page_id, ''))),
                'id '            || lower(TRIM(COALESCE(p.platform_page_id, ''))),
                'page id '       || lower(TRIM(COALESCE(p.platform_page_id, '')))
            ))
    )
"""


def unresolved_reason(page_name: Any, platform_page_id: Any) -> str | None:
    """Why this page's identity is weak — v1's ``unresolved_page_reason``.

    Deliberately conservative: a real brand name that happens to contain digits
    ("Japam 24x7") is left alone. Only exact numeric or generated identities
    are called out.
    """
    name = re.sub(r"\s+", " ", str(page_name or "")).strip()
    page_id = re.sub(r"\s+", "", str(platform_page_id or "")).strip()
    if not name:
        return "Missing page name"
    lowered = name.lower()
    if lowered in WEAK_PAGE_LABELS:
        return "Generic Meta label"
    if page_id and lowered == page_id.lower():
        return "Name is the Page ID"
    if name.replace(" ", "").isdigit():
        return "Numeric-only page name"
    if page_id and lowered in {
        f"page {page_id.lower()}",
        f"meta page {page_id.lower()}",
        f"facebook page {page_id.lower()}",
        f"id {page_id.lower()}",
        f"page id {page_id.lower()}",
    }:
        return "Generated Page ID label"
    if re.fullmatch(r"(?:meta|facebook)?\s*page\s*(?:id\s*)?[#:-]?\s*\d{5,}", name, re.I):
        return "Generated Page ID label"
    return None


def unresolved_count() -> int:
    return _int(_row(f"SELECT COUNT(*) AS n FROM pages p WHERE {_UNRESOLVED_WHERE}",
                     WEAK_PAGE_LABELS).get("n"))


def unresolved_pages(limit: int = UNRESOLVED_LIMIT) -> list[dict]:
    rows = _rows(
        f"""
        SELECT p.id                            AS page_id,
               p.platform_page_id,
               p.name,
               p.alias,
               p.url,
               COALESCE(r.live_active, 0)      AS active_ads,
               COALESCE(r.stored_ads, 0)       AS stored_ads
        FROM pages p
        {_AD_ROLLUP}
        WHERE {_UNRESOLVED_WHERE}
        ORDER BY COALESCE(r.live_active, 0) DESC, COALESCE(r.stored_ads, 0) DESC, p.id DESC
        LIMIT ?
        """,
        (*WEAK_PAGE_LABELS, int(limit)),
    )
    for row in rows:
        row["display_name"] = (row.get("name") or "").strip() or str(
            row.get("platform_page_id") or row["page_id"]
        )
        row["reason"] = unresolved_reason(row.get("name"), row.get("platform_page_id")) or (
            "Weak page identity"
        )
    return rows


# ---------------------------------------------------------------------------
# the screen
# ---------------------------------------------------------------------------
def _requested_view() -> str:
    view = (request.args.get("view") or "all").strip().lower()
    return view if view in VIEWS else "all"


def build_overview(view: str = "all") -> dict:
    """Everything the template needs, computed live. No cache, no read models."""
    names_only = view == "names"
    trends = page_trends()
    ranked = ranked_pages(trends)

    if view == "favorites":
        visible = [row for row in ranked if row["is_favorite"]]
    elif view == "scaling":
        visible = [row for row in ranked if row["is_scaling"]]
    else:
        visible = ranked
    visible = visible[:TOP_PAGE_LIMIT]

    # The Names view replaces the whole main panel, so none of the work below
    # would ever be rendered. Skipping it keeps that tab as cheap as it looks.
    if not names_only:
        attach_top_products(visible)

    return {
        "view": view,
        "kpis": kpis(trends),
        "pinned": pinned_pages(ranked),
        "top_pages": visible,
        "total_pages": len(ranked),
        "top_products": [] if names_only else top_products(),
        "recent": [] if names_only else recent_research(),
        "unresolved_pages": unresolved_pages() if names_only else [],
        "unresolved_count": unresolved_count(),
    }


@bp.get("/overview")
def index():
    view = _requested_view()
    data = build_overview(view)
    return render_template(
        "overview.html",
        active_nav="overview",
        views=VIEWS,
        **data,
    )


@bp.post("/overview/favorite/<int:page_id>")
def toggle_favorite(page_id: int):
    """Pin / unpin a page — the star in the table, the card in the pin strip.

    A plain form POST + redirect, like every other write in v2. ``page_states``
    is 002_parity.sql's side table: no row means "no flags", so the first pin
    inserts one.
    """
    page = _row("SELECT id, name, alias FROM pages WHERE id = ?", (page_id,))
    if not page:
        flash("That page does not exist.", "error")
        return redirect(url_for("overview.index"))

    now = utc_now()
    with db.transaction():
        db.execute(
            """
            INSERT INTO page_states (page_id, is_favorite, favorite_at, updated_at)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(page_id) DO UPDATE SET
                is_favorite = CASE WHEN page_states.is_favorite = 1 THEN 0 ELSE 1 END,
                favorite_at = CASE WHEN page_states.is_favorite = 1 THEN NULL ELSE excluded.favorite_at END,
                updated_at  = excluded.updated_at
            """,
            (page_id, now, now),
        )
    state = _row("SELECT is_favorite FROM page_states WHERE page_id = ?", (page_id,))
    label = (page.get("alias") or "").strip() or (page.get("name") or "").strip() or f"Page {page_id}"
    flash(
        f"{label} {'pinned to' if _int(state.get('is_favorite')) else 'unpinned from'} the overview.",
        "success",
    )
    return redirect(request.form.get("next") or url_for("overview.index"))


@bp.get("/overview/export.csv")
def export_csv():
    """v1's Export CSV button, same columns as the table it sits above."""
    data = build_overview(_requested_view())
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "rank",
            "page",
            "platform_page_id",
            "group",
            "live_ads",
            "represented_ads",
            "top_product",
            "meta_results",
            "oldest_ad_days",
            "seven_day_growth_pct",
            "favorite",
            "last_verified_at",
        ]
    )
    for row in data["top_pages"]:
        writer.writerow(
            [
                row["rank_position"],
                row["display_name"],
                row.get("platform_page_id") or "",
                row.get("group_name") or "",
                row.get("active_ads") or 0,
                row.get("represented_ads") or 0,
                row.get("top_product") or "",
                row.get("meta_results_text") or "",
                row.get("oldest_days") if row.get("oldest_days") is not None else "",
                row.get("seven_day_growth") or 0,
                "yes" if row.get("is_favorite") else "no",
                row.get("last_verified_at") or "",
            ]
        )
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="overview.csv"'},
    )


__all__ = [
    "bp",
    "build_overview",
    "kpis",
    "page_trends",
    "ranked_pages",
    "top_products",
    "unresolved_pages",
    "unresolved_reason",
]
