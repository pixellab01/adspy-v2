"""Winners (/winners) and Test Queue (/test-queue) — the Decision Pipeline.

Two screens, one file, because they are one workflow: Winners ranks the
products a brand group is actually advertising, you judge one, and it lands on
the Test Queue board as a card that walks queued -> testing -> running ->
winner | killed.

WHAT IS PORTED FROM v1 AND WHAT IS NOT
--------------------------------------
Ported: the layout. v1's mode segment (Simple / Complex / Extract details),
its six formula tabs in its order, its four KPIs, its filter bar (group,
identity, status, limit) and its nine table columns — #, Product, Brand group,
Winner score, Active, Represented, Oldest, New 30d — in that order. The board
is v1's five columns with v1's labels ("Testing (live)") and v1's per-card
action buttons.

NOT ported: ``tabs/winners/scoring.py``. The PRD deferred the scoring formulas,
so nothing here invents a number. Every column on this screen is a COUNT or a
date arithmetic over ``ads`` — evidence, not a model. The formula tabs are
therefore *orderings over that evidence* (Scaled ranks by represented ads,
Breakout by ads started in the last 30 days, and so on); each one is a plain
ORDER BY you can check by eye against the column it sorts. The "Winner score"
column shows the human verdict stored in ``winner_shortlist`` — proven /
rising / watch / weak — and is blank until you judge the row. When the
formulas land they fill that column and nothing else on the screen moves.

``group_id`` is required by the ranking (scores are relative inside a brand
group, as in v1), but unlike v1 we do not open on an empty "select a group"
state: the screen defaults to the largest group so the first paint has data.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Any

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import db
from ..time_utils import utc_now

log = logging.getLogger("adspy2.winners")

bp = Blueprint("winners", __name__)
test_queue_bp = Blueprint("test_queue", __name__)


# ---------------------------------------------------------------------------
# the vocabulary of the screen — v1's, verbatim, including the tooltips
# ---------------------------------------------------------------------------
MODES: tuple[tuple[str, str, str], ...] = (
    ("simple", "Simple Formula", "Equal-weight signal count, scored 0-5"),
    ("complex", "Complex Formula", "Weighted composite, scored 0-100"),
    ("extract", "Extract details", "Winner Landing capture queue"),
)

FORMULAS: tuple[tuple[str, str, str, str], ...] = (
    # key, label, v1 tooltip, the evidence column it orders by
    ("overall", "Overall",
     "Balanced: scale, longevity, replication, freshness and depth", "active"),
    ("scaled", "Scaled",
     "Rewards volume: many active/represented ads and mature spend", "represented"),
    ("breakout", "Breakout",
     "Rewards fast recent growth: new ads in the last 7-30 days", "new30"),
    ("evergreen", "Evergreen",
     "Rewards long-running proven ads aged 90+ days", "oldest"),
    ("replicated", "Replicated",
     "Rewards products run by many independent advertisers", "advertisers"),
    ("revival", "Revival",
     "Rewards older products with fresh new ads (a comeback)", "new30"),
)
FORMULA_KEYS = tuple(item[0] for item in FORMULAS)
FORMULA_SORT = {item[0]: item[3] for item in FORMULAS}

IDENTITIES: tuple[tuple[str, str], ...] = (
    ("safe", "Safe identities"),
    ("confirmed", "Confirmed"),
    ("candidate", "Candidate"),
    ("unknown", "Unknown"),
    ("conflict", "Conflict"),
    ("all", "All identities"),
)

STATUS_FILTERS: tuple[tuple[str, str], ...] = (
    ("all", "All"), ("proven", "Proven"), ("rising", "Rising"),
)

LIMITS: tuple[tuple[int, str], ...] = (
    (10, "Top 10"), (50, "Top 50"), (100, "Top 100"), (200, "Top 200"),
    (500, "Top 500"), (1000, "All ranked (up to 1,000)"),
)

SHORTLIST_STATUSES: tuple[tuple[str, str], ...] = (
    ("proven", "Proven"), ("rising", "Rising"),
    ("watch", "Watch"), ("weak", "Weak"),
)

# Every sortable column: key -> (SQL expression, default direction).
SORTS: dict[str, tuple[str, str]] = {
    "product": ("c.product_name COLLATE NOCASE", "asc"),
    "group": ("c.product_name COLLATE NOCASE", "asc"),
    "score": ("COALESCE(w.score_value, -1)", "desc"),
    "active": ("c.active_ads", "desc"),
    "represented": ("c.represented_ads", "desc"),
    "advertisers": ("c.logical_advertisers", "desc"),
    "oldest": ("c.oldest_age_days", "desc"),
    "new30": ("c.new_30d", "desc"),
}

BOARD: tuple[tuple[str, str], ...] = (
    ("queued", "Queued"), ("testing", "Testing (live)"), ("running", "Running"),
    ("winner", "Winner"), ("killed", "Killed"),
)
BOARD_STATUSES = tuple(key for key, _ in BOARD)

# v1's per-card buttons: (label, next status, tone) for each current status.
BOARD_MOVES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "queued": (("Start test", "testing", "primary"), ("Kill", "killed", "kill")),
    "testing": (("Back", "queued", ""), ("Run", "running", "primary"),
                ("Kill", "killed", "kill")),
    "running": (("Back", "testing", ""), ("Mark winner", "winner", "win"),
                ("Kill", "killed", "kill")),
    "winner": (("Back to running", "running", ""),),
    "killed": (("Re-queue", "queued", "primary"),),
}

STAGE_COLUMN = {
    "queued": "queued_at", "testing": "testing_at", "running": "running_at",
    "winner": "winner_at", "killed": "killed_at",
}


@bp.record_once
def _ensure_session_key(state) -> None:
    """flash() needs a secret key and this blueprint must stand on its own if
    the pages module ever fails to import (same guard as routes/queue.py)."""
    app = state.app
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = (
            os.environ.get("ADSPY2_SECRET_KEY") or secrets.token_hex(32)
        )


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _money(value: Any) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(amount, 2) if amount > 0 else 0.0


def _rows(sql: str, params: Any = ()) -> list[dict]:
    return [dict(row) for row in db.fetch_all(sql, params)]


def _row(sql: str, params: Any = ()) -> dict | None:
    found = db.fetch_one(sql, params)
    return dict(found) if found is not None else None


def _one_of(value: Any, allowed, default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------
def brand_groups() -> list[dict]:
    """Every group with the number of products its pages are advertising —
    exactly what v1 prints inside each <option>."""
    counts = {
        int(row["group_id"]): int(row["product_count"])
        for row in _rows(
            """
            SELECT gp.group_id AS group_id,
                   COUNT(DISTINCT ap.product_id) AS product_count
              FROM group_pages gp
              JOIN ads a         ON a.page_id = gp.page_id
              JOIN ad_products ap ON ap.ad_id = a.id
             GROUP BY gp.group_id
            """
        )
    }
    groups = _rows(
        """
        SELECT g.id AS id, g.name AS name,
               (SELECT COUNT(*) FROM group_pages gp WHERE gp.group_id = g.id) AS page_count
          FROM v_group g
         ORDER BY g.name COLLATE NOCASE
        """
    )
    for group in groups:
        group["product_count"] = counts.get(int(group["id"]), 0)
    return groups


# The candidate set: one row per product advertised by a page in this group,
# with the four evidence columns the table prints. Nothing here is a score.
_CANDIDATE_CTE = """
WITH cand AS (
    SELECT ap.product_id                                        AS product_id,
           COUNT(DISTINCT CASE WHEN a.status = 'active' THEN a.id END)      AS active_ads,
           COALESCE(SUM(CASE WHEN a.status = 'active'
                             THEN COALESCE(NULLIF(a.represented_ad_count, 0), 1)
                             ELSE 0 END), 0)                    AS represented_ads,
           COUNT(DISTINCT a.page_id)                            AS logical_advertisers,
           CAST(MAX(CASE WHEN a.status = 'active'
                         THEN julianday('now') - julianday(a.start_date) END)
                AS INTEGER)                                     AS oldest_age_days,
           COUNT(DISTINCT CASE WHEN a.start_date >= date('now', '-30 day')
                               THEN a.id END)                   AS new_30d,
           COUNT(DISTINCT a.id)                                 AS total_ads,
           MAX(a.start_date)                                    AS newest_start_date
      FROM group_pages gp
      JOIN ads a          ON a.page_id = gp.page_id
      JOIN ad_products ap ON ap.ad_id = a.id
     WHERE gp.group_id = ?
     GROUP BY ap.product_id
)
"""

_CANDIDATE_SELECT = """
SELECT c.product_id                                    AS product_id,
       p.display_name                                  AS product_name,
       p.normalized_name                               AS normalized_name,
       p.domain                                        AS store_domain,
       p.product_url                                   AS product_url,
       p.identity_status                               AS identity_status,
       p.is_hidden                                     AS is_hidden,
       c.active_ads, c.represented_ads, c.logical_advertisers,
       c.oldest_age_days, c.new_30d, c.total_ads, c.newest_start_date,
       w.winner_status                                 AS winner_status,
       w.score_value                                   AS score_value,
       w.score_display                                 AS score_display,
       w.score_mode                                    AS score_mode,
       w.assigned_to                                   AS assigned_to,
       w.target_cpp                                    AS target_cpp,
       w.updated_at                                    AS judged_at,
       t.id                                            AS queue_item_id,
       t.queue_status                                  AS queue_status
  FROM cand c
  JOIN v_product p            ON p.id = c.product_id
  LEFT JOIN winner_shortlist w ON w.group_id = ? AND w.product_id = c.product_id
  LEFT JOIN test_queue_items t ON t.group_id = ? AND t.product_id = c.product_id
                              AND t.deleted_at IS NULL
"""


def _identity_clause(identity: str) -> tuple[str, list[Any]]:
    """v1's identity filter. 'safe' is everything whose advertiser identity is
    not actively contradictory — with product_meta empty every product reads
    'unknown', and hiding the whole catalogue behind a default would be a bug
    dressed up as a filter."""
    if identity == "all":
        return "", []
    if identity == "safe":
        return " AND COALESCE(p.identity_status, 'unknown') <> 'conflict'", []
    return " AND COALESCE(p.identity_status, 'unknown') = ?", [identity]


def rank_candidates(
    group_id: int,
    *,
    formula: str = "overall",
    identity: str = "safe",
    status: str = "all",
    search: str = "",
    sort: str = "",
    direction: str = "",
    limit: int = 50,
    min_active: int | None = None,
    min_represented: int | None = None,
    min_pages: int | None = None,
    min_age: int | None = None,
) -> dict:
    """The ranked list plus the counts the KPI row and the filter bar print."""
    where = [
        "c.active_ads > 0",
        "COALESCE(p.is_hidden, 0) = 0",
        "NOT EXISTS (SELECT 1 FROM product_removed r"
        "            WHERE r.normalized_name = p.normalized_name)",
    ]
    params: list[Any] = [group_id, group_id, group_id]

    identity_sql, identity_params = _identity_clause(identity)
    tail = identity_sql
    params.extend(identity_params)

    if formula == "revival":
        # A comeback is an old product with new ads. That is a filter over two
        # real columns, not a weighting of them.
        where.append("COALESCE(c.oldest_age_days, 0) >= 90")
    if status in ("proven", "rising"):
        where.append("w.winner_status = ?")
        params.append(status)
    if search.strip():
        where.append(
            "(p.display_name LIKE ? COLLATE NOCASE"
            " OR COALESCE(p.domain, '') LIKE ? COLLATE NOCASE)"
        )
        needle = f"%{search.strip()}%"
        params.extend([needle, needle])
    for value, column in (
        (min_active, "c.active_ads"),
        (min_represented, "c.represented_ads"),
        (min_pages, "c.logical_advertisers"),
        (min_age, "COALESCE(c.oldest_age_days, 0)"),
    ):
        if value is not None:
            where.append(f"{column} >= ?")
            params.append(int(value))

    sort_key = sort if sort in SORTS else FORMULA_SORT.get(formula, "active")
    expression, natural = SORTS[sort_key]
    order = "asc" if str(direction or natural).lower() == "asc" else "desc"

    body = (
        _CANDIDATE_CTE + _CANDIDATE_SELECT
        + " WHERE " + " AND ".join(where) + tail
    )
    items = _rows(
        f"{body} ORDER BY {expression} {order.upper()},"
        f" c.active_ads DESC, c.product_id LIMIT ?",
        [*params, max(1, min(int(limit), 1000))],
    )
    total = _int(
        (_row(f"SELECT COUNT(*) AS n FROM ({body})", params) or {}).get("n")
    )

    verdicts = {
        str(row["winner_status"]): int(row["n"])
        for row in _rows(
            """
            SELECT winner_status, COUNT(*) AS n
              FROM winner_shortlist WHERE group_id = ?
             GROUP BY winner_status
            """,
            (group_id,),
        )
    }
    evaluated = _int(
        (
            _row(
                _CANDIDATE_CTE
                + "SELECT COUNT(*) AS n FROM cand c JOIN v_product p ON p.id = c.product_id"
                  " WHERE c.active_ads > 0 AND COALESCE(p.is_hidden, 0) = 0",
                (group_id,),
            )
            or {}
        ).get("n")
    )

    for rank, item in enumerate(items, start=1):
        item["rank"] = rank
        item["oldest_age_days"] = _int(item.get("oldest_age_days"))
        item["target_cpp"] = _money(item.get("target_cpp"))
    return {
        "items": items,
        "total": total,
        "evaluated": evaluated,
        "summary": {
            "proven": verdicts.get("proven", 0),
            "rising": verdicts.get("rising", 0),
            "watch": verdicts.get("watch", 0),
            "weak": verdicts.get("weak", 0),
        },
        "sort": sort_key,
        "direction": order,
    }


def candidate(group_id: int, product_id: int) -> dict | None:
    """One ranked row, with the evidence the drawer and the push both need."""
    found = _row(
        _CANDIDATE_CTE + _CANDIDATE_SELECT + " WHERE c.product_id = ?",
        (group_id, group_id, group_id, product_id),
    )
    if found is None:
        return None
    found["oldest_age_days"] = _int(found.get("oldest_age_days"))
    found["target_cpp"] = _money(found.get("target_cpp"))
    found["group"] = _row("SELECT id, name FROM v_group WHERE id = ?", (group_id,))
    found["top_pages"] = _rows(
        """
        SELECT a.page_id                AS page_id,
               pg.name                  AS page_name,
               pg.platform_page_id      AS platform_page_id,
               COUNT(DISTINCT a.id)     AS ads
          FROM group_pages gp
          JOIN ads a          ON a.page_id = gp.page_id AND a.status = 'active'
          JOIN ad_products ap ON ap.ad_id = a.id
          JOIN pages pg       ON pg.id = a.page_id
         WHERE gp.group_id = ? AND ap.product_id = ?
         GROUP BY a.page_id
         ORDER BY ads DESC, pg.name COLLATE NOCASE
         LIMIT 8
        """,
        (group_id, product_id),
    )
    return found


def landing_runs(group_id: int) -> list[dict]:
    """The "Extract details" queue: landing-page capture runs for this group."""
    return _rows(
        """
        SELECT r.*,
               (SELECT COUNT(*) FROM landing_jobs j WHERE j.run_id = r.id) AS jobs
          FROM landing_runs r
         WHERE r.group_id = ?
         ORDER BY r.id DESC
         LIMIT 25
        """,
        (group_id,),
    )


# ---------------------------------------------------------------------------
# Winners screen
# ---------------------------------------------------------------------------
def _selected_group(groups: list[dict]) -> int:
    """The group in the query string, or the biggest one. v1 opens on an empty
    "select a brand group" panel; with four groups and 12,971 active ads in the
    database that is a blank screen for no reason."""
    wanted = _int(request.args.get("group_id"))
    known = {int(group["id"]) for group in groups}
    if wanted in known:
        return wanted
    if not groups:
        return 0
    return int(max(groups, key=lambda group: group["product_count"])["id"])


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    parsed = _int(value, -1)
    return None if parsed < 0 else parsed


@bp.get("/winners")
def winners_page():
    groups = brand_groups()
    group_id = _selected_group(groups)
    mode = _one_of(request.args.get("mode"), {key for key, _, _ in MODES}, "simple")
    formula = _one_of(request.args.get("formula"), FORMULA_KEYS, "overall")
    identity = _one_of(
        request.args.get("identity"), {key for key, _ in IDENTITIES}, "safe"
    )
    status = _one_of(
        request.args.get("status"), {key for key, _ in STATUS_FILTERS}, "all"
    )
    limit = _int(request.args.get("limit"), 50)
    if limit not in {value for value, _ in LIMITS}:
        limit = 50

    data = {"items": [], "total": 0, "evaluated": 0,
            "summary": {"proven": 0, "rising": 0, "watch": 0, "weak": 0},
            "sort": "active", "direction": "desc"}
    if group_id:
        data = rank_candidates(
            group_id,
            formula=formula,
            identity=identity,
            status=status,
            search=request.args.get("q", ""),
            sort=request.args.get("sort", ""),
            direction=request.args.get("dir", ""),
            limit=limit,
            min_active=_optional_int(request.args.get("min_active")),
            min_represented=_optional_int(request.args.get("min_represented")),
            min_pages=_optional_int(request.args.get("min_pages")),
            min_age=_optional_int(request.args.get("min_age")),
        )

    # Jinja has no list comprehensions, so the three link sets are built here.
    # They are tuples of (key, label, href) — data for a macro, not markup.
    query = request.args.get("q", "")
    base = {"group_id": group_id, "mode": mode, "formula": formula,
            "identity": identity, "status": status, "limit": limit,
            "q": query or None}

    def link(**overrides) -> str:
        return url_for("winners.winners_page", **{**base, **overrides})

    sort_links = {}
    for key in SORTS:
        natural = SORTS[key][1]
        flipped = "asc" if (data["sort"] == key and data["direction"] == "desc") else (
            "desc" if data["sort"] == key else natural
        )
        sort_links[key] = {
            "href": link(sort=key, dir=flipped),
            "on": data["sort"] == key,
            "dir": data["direction"],
        }

    return render_template(
        "winners.html",
        active_nav="winners",
        groups=groups,
        group_id=group_id,
        group=next((g for g in groups if int(g["id"]) == group_id), None),
        mode=mode,
        formula=formula,
        identity=identity,
        status=status,
        limit=limit,
        q=query,
        mode_tabs=[(key, label, link(mode=key)) for key, label, _hint in MODES],
        formula_tabs=[(key, label, link(formula=key))
                      for key, label, _hint, _column in FORMULAS],
        sort_links=sort_links,
        identities=IDENTITIES,
        status_filters=STATUS_FILTERS,
        limits=LIMITS,
        runs=landing_runs(group_id) if group_id else [],
        **data,
    )


@bp.get("/ui/winners/<int:group_id>/<int:product_id>")
def winner_detail_fragment(group_id: int, product_id: int):
    """The drawer body — a server-rendered fragment, never markup built in JS."""
    item = candidate(group_id, product_id)
    if item is None:
        abort(404)
    return render_template(
        "winners.html",
        fragment="detail",
        item=item,
        statuses=SHORTLIST_STATUSES,
        mode=_one_of(request.args.get("mode"), {key for key, _, _ in MODES}, "simple"),
        formula=_one_of(request.args.get("formula"), FORMULA_KEYS, "overall"),
        back=request.args.get("back") or url_for("winners.winners_page",
                                                 group_id=group_id),
    )


def _upsert_shortlist(group_id: int, product_id: int, form) -> dict:
    now = utc_now()
    verdict = _one_of(
        form.get("winner_status"), {key for key, _ in SHORTLIST_STATUSES}, "watch"
    )
    mode = _one_of(form.get("mode"), {"simple", "complex"}, "simple")
    formula = _one_of(form.get("formula"), FORMULA_KEYS, "overall")
    assigned = str(form.get("assigned_to") or "").strip()[:120]
    target_cpp = _money(form.get("target_cpp"))
    with db.transaction():
        db.execute(
            """
            INSERT INTO winner_shortlist
                (group_id, product_id, winner_status, score_mode, score_formula,
                 score_value, score_display, assigned_to, target_cpp,
                 submitted_at, updated_at)
            VALUES (?,?,?,?,?,0,NULL,?,?,?,?)
            ON CONFLICT(group_id, product_id) DO UPDATE SET
                winner_status = excluded.winner_status,
                score_mode    = excluded.score_mode,
                score_formula = excluded.score_formula,
                assigned_to   = excluded.assigned_to,
                target_cpp    = excluded.target_cpp,
                updated_at    = excluded.updated_at
            """,
            (group_id, product_id, verdict, mode, formula, assigned,
             target_cpp, now, now),
        )
    return {"winner_status": verdict, "assigned_to": assigned,
            "target_cpp": target_cpp}


@bp.post("/winners/<int:group_id>/<int:product_id>/shortlist")
def save_shortlist(group_id: int, product_id: int):
    item = candidate(group_id, product_id)
    if item is None:
        flash("That product is not advertised by this brand group.", "error")
        return redirect(url_for("winners.winners_page", group_id=group_id))
    saved = _upsert_shortlist(group_id, product_id, request.form)

    if request.form.get("action") == "push":
        return _push_to_test_queue(group_id, item, saved)

    flash(
        f"{item['product_name']} marked {saved['winner_status']}.", "success"
    )
    return redirect(request.form.get("next")
                    or url_for("winners.winners_page", group_id=group_id))


def _push_to_test_queue(group_id: int, item: dict, saved: dict):
    """Freeze the evidence and put the product on the board.

    Frozen on purpose: the board is a record of what you decided on and when.
    Re-pushing a product that is already there updates the evidence and leaves
    its stage alone (v1's rule, README: "already in Test Queue is updated
    instead of duplicated, while its current queue stage is kept")."""
    now = utc_now()
    source = (item.get("top_pages") or [{}])[0]
    group_name = (item.get("group") or {}).get("name") or ""
    existing = _row(
        "SELECT id, queue_status, deleted_at FROM test_queue_items"
        " WHERE group_id = ? AND product_id = ?",
        (group_id, item["product_id"]),
    )
    with db.transaction():
        if existing:
            db.execute(
                """
                UPDATE test_queue_items
                   SET product_name = ?, group_name = ?, product_domain = ?,
                       external_product_url = ?, source_page_id = ?,
                       source_page_name = ?, buyer_name = ?, target_cpp = ?,
                       winner_status = ?, active_ads = ?, represented_ads = ?,
                       logical_advertisers = ?, oldest_active_days = ?,
                       new_ads_30d = ?, deleted_at = NULL, updated_at = ?
                 WHERE id = ?
                """,
                (item["product_name"], group_name, item.get("store_domain") or "",
                 item.get("product_url") or "", source.get("page_id"),
                 source.get("page_name") or "", saved["assigned_to"],
                 saved["target_cpp"], saved["winner_status"],
                 _int(item["active_ads"]), _int(item["represented_ads"]),
                 _int(item["logical_advertisers"]), _int(item["oldest_age_days"]),
                 _int(item["new_30d"]), now, int(existing["id"])),
            )
            item_id = int(existing["id"])
        else:
            cursor = db.execute(
                """
                INSERT INTO test_queue_items
                    (group_id, product_id, product_name, group_name,
                     product_domain, external_product_url, source_page_id,
                     source_page_name, buyer_name, target_cpp, queue_status,
                     simple_score, complex_score, winner_status, active_ads,
                     represented_ads, logical_advertisers, oldest_active_days,
                     new_ads_30d, queued_at, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,'queued',0,0,?,?,?,?,?,?,?,?,?)
                """,
                (group_id, item["product_id"], item["product_name"], group_name,
                 item.get("store_domain") or "", item.get("product_url") or "",
                 source.get("page_id"), source.get("page_name") or "",
                 saved["assigned_to"], saved["target_cpp"],
                 saved["winner_status"], _int(item["active_ads"]),
                 _int(item["represented_ads"]), _int(item["logical_advertisers"]),
                 _int(item["oldest_age_days"]), _int(item["new_30d"]),
                 now, now, now),
            )
            item_id = int(cursor.lastrowid)
    flash(
        f"{item['product_name']} is on the Test Queue as entry #{item_id}.",
        "success",
    )
    return redirect(url_for("test_queue.test_queue_page"))


@bp.post("/winners/<int:group_id>/<int:product_id>/push")
def push_to_test_queue(group_id: int, product_id: int):
    item = candidate(group_id, product_id)
    if item is None:
        flash("That product is not advertised by this brand group.", "error")
        return redirect(url_for("winners.winners_page", group_id=group_id))
    saved = _upsert_shortlist(group_id, product_id, request.form)
    return _push_to_test_queue(group_id, item, saved)


# ---------------------------------------------------------------------------
# Test Queue screen
# ---------------------------------------------------------------------------
def board_items(deleted: bool = False) -> list[dict]:
    clause = "IS NOT NULL" if deleted else "IS NULL"
    order = "t.deleted_at DESC" if deleted else "t.updated_at DESC, t.id DESC"
    return _rows(
        f"""
        SELECT t.*,
               pg.platform_page_id AS source_platform_page_id
          FROM test_queue_items t
          LEFT JOIN pages pg ON pg.id = t.source_page_id
         WHERE t.deleted_at {clause}
         ORDER BY {order}
        """
    )


def queue_item(item_id: int) -> dict | None:
    return _row(
        """
        SELECT t.*, pg.platform_page_id AS source_platform_page_id
          FROM test_queue_items t
          LEFT JOIN pages pg ON pg.id = t.source_page_id
         WHERE t.id = ?
        """,
        (item_id,),
    )


@test_queue_bp.get("/test-queue")
def test_queue_page():
    deleted = request.args.get("deleted") == "1"
    items = board_items(deleted=deleted)
    columns = {key: [] for key in BOARD_STATUSES}
    for item in items:
        columns.setdefault(str(item["queue_status"]), []).append(item)
    deleted_count = _int(
        (
            _row(
                "SELECT COUNT(*) AS n FROM test_queue_items"
                " WHERE deleted_at IS NOT NULL"
            )
            or {}
        ).get("n")
    )
    return render_template(
        "test_queue.html",
        active_nav="test_queue",
        board=BOARD,
        moves=BOARD_MOVES,
        columns=columns,
        items=items,
        deleted=deleted,
        deleted_count=deleted_count,
        live_count=len(items) if not deleted else 0,
    )


@test_queue_bp.get("/ui/test-queue/<int:item_id>")
def queue_detail_fragment(item_id: int):
    item = queue_item(item_id)
    if item is None:
        abort(404)
    return render_template("test_queue.html", fragment="detail", item=item,
                           board=BOARD, moves=BOARD_MOVES)


def _redirect_back():
    return redirect(request.form.get("next")
                    or url_for("test_queue.test_queue_page"))


@test_queue_bp.post("/test-queue/<int:item_id>/status")
def move_item(item_id: int):
    item = queue_item(item_id)
    if item is None:
        flash(f"Test Queue entry #{item_id} does not exist.", "error")
        return _redirect_back()
    target = _one_of(request.form.get("status"), BOARD_STATUSES, "")
    if not target:
        flash("Unknown queue stage.", "error")
        return _redirect_back()

    now = utc_now()
    column = STAGE_COLUMN[target]
    with db.transaction():
        db.execute(
            f"""
            UPDATE test_queue_items
               SET queue_status = ?, {column} = COALESCE({column}, ?),
                   updated_at = ?
             WHERE id = ?
            """,
            (target, now, now, item_id),
        )
    flash(f"#{item_id} {item['product_name']} -> {target}.", "success")
    return _redirect_back()


@test_queue_bp.post("/test-queue/<int:item_id>/delete")
def delete_item(item_id: int):
    item = queue_item(item_id)
    if item is None:
        flash(f"Test Queue entry #{item_id} does not exist.", "error")
        return _redirect_back()
    permanent = request.form.get("permanent") == "1"
    with db.transaction():
        if permanent:
            db.execute("DELETE FROM test_queue_items WHERE id = ?", (item_id,))
        else:
            db.execute(
                "UPDATE test_queue_items SET deleted_at = ?, updated_at = ?"
                " WHERE id = ?",
                (utc_now(), utc_now(), item_id),
            )
    flash(
        f"#{item_id} {item['product_name']} "
        + ("deleted permanently." if permanent else "moved to Deleted."),
        "success",
    )
    return _redirect_back()


@test_queue_bp.post("/test-queue/<int:item_id>/restore")
def restore_item(item_id: int):
    item = queue_item(item_id)
    if item is None:
        flash(f"Test Queue entry #{item_id} does not exist.", "error")
        return _redirect_back()
    with db.transaction():
        db.execute(
            "UPDATE test_queue_items SET deleted_at = NULL, updated_at = ?"
            " WHERE id = ?",
            (utc_now(), item_id),
        )
    flash(f"#{item_id} {item['product_name']} restored.", "success")
    return _redirect_back()


__all__ = ["bp", "test_queue_bp", "rank_candidates", "candidate", "board_items"]
