"""Reads and writes for the Products screen and the Brand Groups screen.

Both screens sit on the same three joins — ``products`` -> ``ad_products`` ->
``ads`` -> ``pages`` — so they share one module rather than the two 74k/186k
line ``tabs/*/queries.py`` files v1 ended up with.

Conventions copied from ``app/queries.py`` (do not diverge from them):

* every number is counted from ``ads`` at request time; there is no cache
  table to invalidate,
* every function returns plain ``dict``s carrying the derived fields the
  templates need, so templates never do arithmetic,
* sort keys, sections and filter values are looked up in whitelists — nothing
  from the querystring is ever concatenated into SQL.

The numbers, and what they mean (v1's definitions, verbatim):

    active_ad_count      distinct ads whose status is 'active'
    represented_ad_count sum of ads.represented_ad_count over active ads —
                         Meta collapses versions, so one "box" can be 6 ads
    age_days             days since the OLDEST ad's Meta start_date. Never the
                         date we scraped it: v1's product tab says so on the
                         filter modal and it is the whole point of the screen.
    unique scripts       distinct script_clusters behind a product's videos.
                         "50 ads are really 5 videos" — the insight the tool
                         exists for.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Iterable, Sequence

from . import db
from .meta_links import normalize_product_url, product_display_name_from_url
from .time_utils import utc_now

# --- products ---------------------------------------------------------------
PAGE_SIZE = 36                                    # v1 tabs/product/advanced.py
MAX_PAGE_SIZE = 120

PRODUCT_SECTIONS = (
    "page_wise", "page_sets", "full", "tracked", "saved", "favorite", "hidden",
)
DEFAULT_PRODUCT_SECTION = "page_wise"

# key -> ORDER BY clause. v1 tabs/product/advanced.py:648-657, verbatim.
PRODUCT_SORTS: dict[str, str] = {
    "active_desc": "active_ad_count DESC, represented_ad_count DESC, age_days DESC, id DESC",
    "represented_desc": "represented_ad_count DESC, active_ad_count DESC, age_days DESC, id DESC",
    "age_oldest": "CASE WHEN age_days IS NULL THEN 1 ELSE 0 END, age_days DESC, active_ad_count DESC",
    "age_newest": "CASE WHEN age_days IS NULL THEN 1 ELSE 0 END, age_days ASC, active_ad_count DESC",
    "pages_desc": "page_count DESC, active_ad_count DESC, product_name COLLATE NOCASE ASC",
    "name_asc": "product_name COLLATE NOCASE ASC, id ASC",
    "name_desc": "product_name COLLATE NOCASE DESC, id DESC",
    "store_asc": "store_domain COLLATE NOCASE ASC, product_name COLLATE NOCASE ASC",
}
DEFAULT_PRODUCT_SORT = "active_desc"

# The select's option list, in v1's order (tabs/product/ui.py:601).
PRODUCT_SORT_OPTIONS = (
    ("active_desc", "Active ads: high to low"),
    ("represented_desc", "Represented: high to low"),
    ("age_oldest", "Ad age: oldest first"),
    ("age_newest", "Ad age: newest first"),
    ("pages_desc", "Pages: high to low"),
    ("name_asc", "Product: A to Z"),
    ("name_desc", "Product: Z to A"),
    ("store_asc", "Store: A to Z"),
)

AD_STATUS_OPTIONS = (("all", "All statuses"), ("active", "Active only"),
                     ("inactive", "Inactive only"))
MEDIA_TYPE_OPTIONS = (("all", "All media"), ("video", "Video"), ("image", "Image"),
                      ("carousel", "Carousel"), ("unknown", "Unknown"))
HAS_URL_OPTIONS = (("all", "Any URL"), ("yes", "Has URL"), ("no", "No URL"))

PRODUCT_STATE_FIELDS = ("tracked", "saved", "favorite", "hidden")

# --- linked-ads column picker (v1 tabs/brand_group/ui.py:1029-1044) ----------
AD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ad_id", "Ad ID"),
    ("library_id", "Library ID"),
    ("ad_library_url", "Ad Library URL"),
    ("meta_page_id", "Meta Page ID"),
    ("advertiser", "Advertiser"),
    ("advertiser_url", "Advertiser URL"),
    ("status", "Status"),
    ("age", "Age"),
    ("format", "Format"),
    ("language", "Language"),
    ("script_id", "Script"),
    ("start_date", "Start date"),
)
AD_COLUMN_KEYS = tuple(key for key, _ in AD_COLUMNS)
AD_COLUMN_LABELS = dict(AD_COLUMNS)
DEFAULT_AD_COLUMNS = (
    "ad_id", "library_id", "meta_page_id", "advertiser", "status", "age",
    "format", "language", "script_id",
)

# Only the languages this database actually holds, plus the obvious neighbours.
LANGUAGE_NAMES = {
    "hi": "Hindi", "en": "English", "ta": "Tamil", "mr": "Marathi",
    "ml": "Malayalam", "te": "Telugu", "kn": "Kannada", "bn": "Bengali",
    "gu": "Gujarati", "pa": "Punjabi", "or": "Odia", "ur": "Urdu",
    "und": "Unknown",
}

# --- brand groups -----------------------------------------------------------
GROUP_SORTS = {
    "live_desc": "live_ads DESC, page_count DESC, name COLLATE NOCASE ASC",
    "pages_desc": "page_count DESC, live_ads DESC, name COLLATE NOCASE ASC",
    "name_asc": "name COLLATE NOCASE ASC",
    "updated_desc": "COALESCE(g.updated_at, g.created_at) DESC, name COLLATE NOCASE ASC",
}
DEFAULT_GROUP_SORT = "live_desc"
GROUP_SORT_OPTIONS = (("live_desc", "Active"), ("pages_desc", "Pages"),
                      ("name_asc", "A-Z"), ("updated_desc", "Updated"))

GROUP_PAGE_CATEGORIES = (
    ("all", "All pages"), ("multiple", "Multiple records"), ("single", "Single record"),
    ("evergreen", "Evergreen"), ("product_heavy", "Product heavy"),
    ("video", "Video-led"), ("weak", "Weak"),
)
GROUP_PAGE_ORDERS = (
    ("live_desc", "Active ads"), ("represented_desc", "Represented"),
    ("products_desc", "Products"), ("oldest_desc", "Oldest"),
    ("sources_desc", "Source records"), ("name_asc", "Name"),
)
GROUP_PRODUCT_VISIBILITY = (("visible", "Visible products"), ("hidden", "Hidden products"))
GROUP_PRODUCT_SORTS = (
    ("active_desc", "Active ads"), ("represented_desc", "Represented"),
    ("pages_desc", "Pages"), ("age_oldest", "Oldest"), ("age_newest", "Newest"),
    ("video_desc", "Video"), ("image_desc", "Image"), ("name_asc", "Name"),
)
GROUP_PRODUCT_AGES = (
    ("all", "Any age"), ("age_0_30", "0-30 days"), ("age_31_90", "31-90 days"),
    ("age_91_180", "91-180 days"), ("age_181_365", "181-365 days"),
    ("age_365_plus", "365+ days"), ("unknown", "Unknown"),
)
GROUP_PAGE_SCOPES = (("ungrouped", "Ungrouped"), ("all", "All pages"),
                     ("current", "Current group"))
GROUP_PICKER_SORTS = (("active_desc", "Active ads"),
                      ("represented_desc", "Represented"), ("name_asc", "Name"))

GROUP_PAGE_SIZE = 40


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _rows(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in db.fetch_all(sql, params)]


def _row(sql: str, params: Iterable[Any] = ()) -> dict | None:
    found = db.fetch_one(sql, params)
    return dict(found) if found is not None else None


def _placeholders(values: Sequence[Any]) -> str:
    return ",".join("?" for _ in values)


def clean_text(value: Any, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def positive_int(value: Any, default: int = 1) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def optional_int(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = int(float(text))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def id_list(values: Iterable[Any] | None, limit: int = 400) -> list[int]:
    """Whitelist a list of ids coming from a querystring or a form."""
    out: list[int] = []
    seen: set[int] = set()
    for raw in values or ():
        for part in re.split(r"[,\s]+", str(raw or "")):
            if not part.isdigit():
                continue
            number = int(part)
            if number <= 0 or number in seen:
                continue
            seen.add(number)
            out.append(number)
            if len(out) >= limit:
                return out
    return out


def language_name(code: Any) -> str:
    text = str(code or "").strip().lower()
    if not text:
        return "Unknown"
    return LANGUAGE_NAMES.get(text, text.upper())


def resolve_columns(raw: Any) -> list[str]:
    """``cols=ad_id,language`` -> a whitelisted, de-duplicated column order.

    Unknown keys are dropped and an empty result falls back to v1's default
    set, so a hand-edited URL can never render a table with no columns.
    """
    picked: list[str] = []
    for part in re.split(r"[,\s]+", str(raw or "")):
        key = part.strip()
        if key in AD_COLUMN_KEYS and key not in picked:
            picked.append(key)
    return picked or list(DEFAULT_AD_COLUMNS)


def toggle_column(current: Sequence[str], key: str) -> list[str]:
    """The `cols` value a Columns-picker checkbox should link to."""
    if key not in AD_COLUMN_KEYS:
        return list(current)
    if key in current:
        remaining = [col for col in current if col != key]
        return remaining or list(current)          # never empty the table
    # Put it back where v1's picker keeps it: the master order.
    order = {name: index for index, name in enumerate(AD_COLUMN_KEYS)}
    return sorted([*current, key], key=lambda name: order[name])


def move_column(current: Sequence[str], key: str, direction: int) -> list[str]:
    cols = list(current)
    if key not in cols:
        return cols
    index = cols.index(key)
    target = index + (-1 if direction < 0 else 1)
    if target < 0 or target >= len(cols):
        return cols
    cols[index], cols[target] = cols[target], cols[index]
    return cols


def _paginate(total: int, page: int, per_page: int) -> dict:
    pages = max(1, math.ceil(total / per_page)) if per_page else 1
    page = min(max(1, page), pages)
    offset = (page - 1) * per_page
    return {
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "offset": offset,
        "total": total,
        "start": offset + 1 if total else 0,
        "end": min(offset + per_page, total),
        "has_previous": page > 1,
        "has_next": page < pages,
    }


# ---------------------------------------------------------------------------
# the product stats CTE — one definition, used by list, detail and export
# ---------------------------------------------------------------------------
_ACTIVE = "lower(COALESCE(a.status,'active')) = 'active'"

_PRODUCT_STATS = f"""
WITH links AS (
    SELECT DISTINCT product_id, ad_id
    FROM ad_products
    WHERE product_id IS NOT NULL AND ad_id IS NOT NULL
),
stats AS (
    SELECT
        pr.id                                        AS id,
        pr.display_name                              AS product_name,
        pr.normalized_name                           AS normalized_name,
        pr.product_url                               AS product_url,
        pr.domain                                    AS store_domain,
        pr.shortlist_state                           AS shortlist_state,
        pr.first_seen_at                             AS first_seen_at,
        pr.last_seen_at                              AS last_seen_at,
        m.category                                   AS category,
        m.product_type                               AS product_type,
        m.store_platform                             AS store_platform,
        COALESCE(NULLIF(m.canonical_url, ''), NULLIF(pr.product_url, ''),
                 MAX(NULLIF(a.destination_url, ''))) AS product_link,
        COUNT(DISTINCT a.id)                         AS ad_count,
        COUNT(DISTINCT CASE WHEN {_ACTIVE} THEN a.id END)   AS active_ad_count,
        COALESCE(SUM(CASE WHEN {_ACTIVE}
                          THEN MAX(1, COALESCE(a.represented_ad_count, 1))
                          ELSE 0 END), 0)            AS represented_ad_count,
        COUNT(DISTINCT a.page_id)                    AS page_count,
        COUNT(DISTINCT CASE WHEN lower(COALESCE(a.media_type,'')) = 'video'
                            THEN a.id END)           AS video_ads,
        COUNT(DISTINCT CASE WHEN lower(COALESCE(a.media_type,'')) = 'image'
                            THEN a.id END)           AS image_ads,
        GROUP_CONCAT(DISTINCT a.media_type)          AS media_types,
        MIN(date(a.start_date))                      AS oldest_start_date,
        MAX(date(a.start_date))                      AS newest_start_date,
        CASE WHEN MIN(date(a.start_date)) IS NULL THEN NULL
             ELSE MAX(1, CAST(julianday('now') - julianday(MIN(date(a.start_date)))
                              AS INTEGER) + 1) END   AS age_days,
        CASE WHEN MAX(date(a.start_date)) IS NULL THEN NULL
             ELSE MAX(1, CAST(julianday('now') - julianday(MAX(date(a.start_date)))
                              AS INTEGER) + 1) END   AS newest_age_days,
        COALESCE(st.tracked, 0)                      AS is_tracked,
        COALESCE(st.saved, 0)                        AS is_saved,
        COALESCE(st.favorite, 0)                     AS is_favorite,
        COALESCE(st.hidden, 0)                       AS is_hidden
    FROM products pr
    JOIN links dl        ON dl.product_id = pr.id
    JOIN ads a           ON a.id = dl.ad_id
    LEFT JOIN product_states st ON st.product_id = pr.id
    LEFT JOIN product_meta  m   ON m.product_id  = pr.id
    WHERE {{inner_where}}
    GROUP BY pr.id
)
SELECT * FROM stats WHERE {{outer_where}}
"""


def _product_filter_sql(
    *,
    search: str,
    page_ids: Sequence[int],
    ad_status: str,
    media_type: str,
    product_id: int | None,
) -> tuple[list[str], list[Any]]:
    where = ["1 = 1"]
    params: list[Any] = []

    if product_id:
        where.append("pr.id = ?")
        params.append(int(product_id))

    if page_ids:
        where.append(f"a.page_id IN ({_placeholders(page_ids)})")
        params.extend(page_ids)

    if ad_status == "active":
        where.append(_ACTIVE)
    elif ad_status == "inactive":
        where.append(f"NOT ({_ACTIVE})")

    if media_type and media_type != "all":
        where.append("lower(COALESCE(a.media_type,'')) = ?")
        params.append(media_type)

    needle = clean_text(search).lower()
    if needle:
        like = f"%{needle}%"
        scope = ""
        scope_params: list[Any] = []
        if page_ids:
            scope = f" AND sa.page_id IN ({_placeholders(page_ids)})"
            scope_params.extend(page_ids)
        where.append(
            "("
            "lower(COALESCE(pr.display_name,'')) LIKE ? OR "
            "lower(COALESCE(pr.normalized_name,'')) LIKE ? OR "
            "lower(COALESCE(pr.domain,'')) LIKE ? OR "
            "lower(COALESCE(pr.product_url,'')) LIKE ? OR "
            "EXISTS (SELECT 1 FROM ad_products sap "
            "        JOIN ads sa ON sa.id = sap.ad_id "
            "        LEFT JOIN pages spg ON spg.id = sa.page_id "
            f"       WHERE sap.product_id = pr.id{scope} AND ("
            "           lower(COALESCE(spg.name,'')) LIKE ? OR "
            "           lower(COALESCE(spg.alias,'')) LIKE ? OR "
            "           lower(COALESCE(spg.platform_page_id,'')) LIKE ? OR "
            "           lower(COALESCE(sa.headline,'')) LIKE ?))"
            ")"
        )
        params.extend([like, like, like, like, *scope_params, like, like, like, like])
    return where, params


def _product_outer_sql(
    *, section: str, product_id: int | None, filters: dict
) -> tuple[list[str], list[Any]]:
    where = ["1 = 1"]
    params: list[Any] = []

    if section == "hidden":
        where.append("is_hidden = 1")
    elif product_id is None:
        where.append("is_hidden = 0")
        if section == "tracked":
            where.append("is_tracked = 1")
        elif section == "saved":
            where.append("is_saved = 1")
        elif section == "favorite":
            where.append("is_favorite = 1")

    numeric = (
        ("active_ad_count", "min_active", ">="),
        ("active_ad_count", "max_active", "<="),
        ("represented_ad_count", "min_represented", ">="),
        ("represented_ad_count", "max_represented", "<="),
        ("age_days", "min_age", ">="),
        ("age_days", "max_age", "<="),
        ("page_count", "min_pages", ">="),
        ("page_count", "max_pages", "<="),
    )
    for column, key, operator in numeric:
        value = filters.get(key)
        if value is None:
            continue
        where.append(f"{column} {operator} ?")
        params.append(int(value))

    has_url = filters.get("has_url") or "all"
    if has_url == "yes":
        where.append("COALESCE(product_link,'') <> ''")
    elif has_url == "no":
        where.append("COALESCE(product_link,'') = ''")
    return where, params


def normalize_product_filters(source: Any) -> dict:
    """Read the advanced-filter modal's fields off a request.args mapping."""
    get = source.get
    ad_status = clean_text(get("ad_status", "all"), 16).lower()
    media_type = clean_text(get("media_type", "all"), 16).lower()
    has_url = clean_text(get("has_url", "all"), 8).lower()
    return {
        "ad_status": ad_status if ad_status in dict(AD_STATUS_OPTIONS) else "all",
        "media_type": media_type if media_type in dict(MEDIA_TYPE_OPTIONS) else "all",
        "has_url": has_url if has_url in dict(HAS_URL_OPTIONS) else "all",
        "min_active": optional_int(get("min_active")),
        "max_active": optional_int(get("max_active")),
        "min_represented": optional_int(get("min_represented")),
        "max_represented": optional_int(get("max_represented")),
        "min_age": optional_int(get("min_age")),
        "max_age": optional_int(get("max_age")),
        "min_pages": optional_int(get("min_pages")),
        "max_pages": optional_int(get("max_pages")),
    }


def active_filter_count(filters: dict) -> int:
    count = 0
    for key in ("ad_status", "media_type", "has_url"):
        if (filters.get(key) or "all") != "all":
            count += 1
    for key in ("min_active", "max_active", "min_represented", "max_represented",
                "min_age", "max_age", "min_pages", "max_pages"):
        if filters.get(key) is not None:
            count += 1
    return count


def _decorate_product(row: dict) -> dict:
    row["product_name"] = (row.get("product_name") or "").strip() or "Unnamed product"
    row["store_domain"] = (row.get("store_domain") or "").strip()
    row["age_label"] = _age_label(row.get("age_days"))
    row["newest_age_label"] = _age_label(row.get("newest_age_days"))
    row["media_summary"] = ", ".join(
        part for part in sorted(str(row.get("media_types") or "").split(",")) if part
    ) or "media unknown"
    row["oldest_day"] = (row.get("oldest_start_date") or "")[:10]
    row["is_tracked"] = bool(row.get("is_tracked"))
    row["is_saved"] = bool(row.get("is_saved"))
    row["is_favorite"] = bool(row.get("is_favorite"))
    row["is_hidden"] = bool(row.get("is_hidden"))
    row["pages"] = []
    return row


def _age_label(days: Any) -> str:
    if days is None:
        return "—"
    number = int(days)
    if number >= 365:
        years = number / 365.0
        return f"{years:.1f}y"
    return f"{number}d"


# ---------------------------------------------------------------------------
# products — list
# ---------------------------------------------------------------------------
def list_products(
    *,
    section: str = DEFAULT_PRODUCT_SECTION,
    search: str = "",
    sort: str = DEFAULT_PRODUCT_SORT,
    page_ids: Sequence[int] = (),
    filters: dict | None = None,
    page: int = 1,
    per_page: int = PAGE_SIZE,
) -> dict:
    section = section if section in PRODUCT_SECTIONS else DEFAULT_PRODUCT_SECTION
    sort = sort if sort in PRODUCT_SORTS else DEFAULT_PRODUCT_SORT
    filters = filters or normalize_product_filters({})
    per_page = max(1, min(int(per_page or PAGE_SIZE), MAX_PAGE_SIZE))
    scoped = list(page_ids) if section == "page_wise" else []

    inner, inner_params = _product_filter_sql(
        search=search,
        page_ids=scoped,
        ad_status=filters["ad_status"],
        media_type=filters["media_type"],
        product_id=None,
    )
    outer, outer_params = _product_outer_sql(
        section=section, product_id=None, filters=filters
    )
    base = _PRODUCT_STATS.format(
        inner_where=" AND ".join(inner), outer_where=" AND ".join(outer)
    )
    params = [*inner_params, *outer_params]

    total = int(
        (_row(f"SELECT COUNT(*) AS n FROM ({base})", params) or {}).get("n") or 0
    )
    meta = _paginate(total, positive_int(page, 1), per_page)
    rows = _rows(
        f"SELECT * FROM ({base}) ORDER BY {PRODUCT_SORTS[sort]} LIMIT ? OFFSET ?",
        [*params, per_page, meta["offset"]],
    )
    items = [_decorate_product(row) for row in rows]
    attach_advertiser_pages(items, page_ids=scoped)
    attach_product_trends(items)
    return {
        "items": items,
        "section": section,
        "sort": sort,
        "search": clean_text(search),
        "filters": filters,
        "selected_page_ids": list(scoped),
        **meta,
        "end": min(meta["offset"] + len(items), total),
    }


def attach_advertiser_pages(items: list[dict], *, page_ids: Sequence[int] = ()) -> None:
    """Fill each product's ``pages`` list — the Advertiser column's pills."""
    ids = [int(item["id"]) for item in items]
    if not ids:
        return
    where = [f"ap.product_id IN ({_placeholders(ids)})"]
    params: list[Any] = list(ids)
    if page_ids:
        where.append(f"a.page_id IN ({_placeholders(page_ids)})")
        params.extend(page_ids)
    rows = _rows(
        f"""
        SELECT ap.product_id                                   AS product_id,
               p.id                                            AS page_id,
               p.platform_page_id                              AS platform_page_id,
               COALESCE(NULLIF(p.alias,''), NULLIF(p.name,''),
                        'Page ' || p.platform_page_id)         AS page_name,
               COUNT(DISTINCT CASE WHEN {_ACTIVE} THEN a.id END) AS active_ads
        FROM ad_products ap
        JOIN ads a   ON a.id = ap.ad_id
        JOIN pages p ON p.id = a.page_id
        WHERE {' AND '.join(where)}
        GROUP BY ap.product_id, p.id
        ORDER BY active_ads DESC, page_name COLLATE NOCASE ASC
        """,
        params,
    )
    by_product: dict[int, list[dict]] = {}
    for row in rows:
        by_product.setdefault(int(row["product_id"]), []).append(row)
    for item in items:
        item["pages"] = by_product.get(int(item["id"]), [])


def product_summary(page_ids: Sequence[int] = ()) -> dict:
    """The counts in the tab row. ``pages`` is the advertiser-page count."""
    counts = _row(
        """
        SELECT
            COUNT(*)                                          AS total,
            SUM(CASE WHEN is_tracked  = 1 AND is_hidden = 0 THEN 1 ELSE 0 END) AS tracked,
            SUM(CASE WHEN is_saved    = 1 AND is_hidden = 0 THEN 1 ELSE 0 END) AS saved,
            SUM(CASE WHEN is_favorite = 1 AND is_hidden = 0 THEN 1 ELSE 0 END) AS favorite,
            SUM(CASE WHEN is_hidden   = 1 THEN 1 ELSE 0 END)  AS hidden
        FROM (
            SELECT COALESCE(st.tracked,0)  AS is_tracked,
                   COALESCE(st.saved,0)    AS is_saved,
                   COALESCE(st.favorite,0) AS is_favorite,
                   COALESCE(st.hidden,0)   AS is_hidden
            FROM products pr
            LEFT JOIN product_states st ON st.product_id = pr.id
            WHERE EXISTS (SELECT 1 FROM ad_products ap WHERE ap.product_id = pr.id)
        )
        """
    ) or {}
    summary = {key: int(value or 0) for key, value in counts.items()}
    summary["total"] = summary.get("total", 0)
    # "Full List" excludes hidden products, exactly like the query behind it.
    summary["full"] = summary["total"] - summary.get("hidden", 0)
    summary["pages"] = int(
        (_row("SELECT COUNT(*) AS n FROM pages WHERE is_hidden = 0") or {}).get("n") or 0
    )
    summary["page_sets"] = int(
        (_row("SELECT COUNT(*) AS n FROM product_page_sets") or {}).get("n") or 0
    )
    summary["selected"] = len(page_ids)
    return summary


# ---------------------------------------------------------------------------
# products — one product
# ---------------------------------------------------------------------------
def get_product(product_id: int) -> dict | None:
    inner, inner_params = _product_filter_sql(
        search="", page_ids=(), ad_status="all", media_type="all",
        product_id=int(product_id),
    )
    base = _PRODUCT_STATS.format(inner_where=" AND ".join(inner), outer_where="1 = 1")
    row = _row(base, inner_params)
    if row is None:
        return None
    product = _decorate_product(row)
    attach_advertiser_pages([product])
    return product


def product_ads(product_id: int, *, language: str = "", limit: int = 500) -> list[dict]:
    """The Linked ads table. One row per ad, whatever the column picker shows.

    ``language`` and ``script_id`` come from the transcription subsystem:
    ``ad_languages`` when a language was detected from the ad's own text, else
    the language of the transcript of the ad's video. ``script_id`` is the
    script cluster — two ads with the same S-number are the same video script.
    """
    params: list[Any] = [int(product_id)]
    having = ""
    if language:
        having = "HAVING lower(COALESCE(language, 'und')) = ?"
    sql = f"""
        SELECT
            a.id                                            AS ad_id,
            a.library_id                                    AS library_id,
            a.status                                        AS status,
            a.start_date                                    AS start_date,
            a.media_type                                    AS media_type,
            a.headline                                      AS headline,
            a.destination_url                               AS destination_url,
            p.id                                            AS page_record_id,
            p.platform_page_id                              AS platform_page_id,
            COALESCE(NULLIF(p.alias,''), NULLIF(p.name,''),
                     'Page ' || COALESCE(p.platform_page_id,'?')) AS page_name,
            p.url                                           AS advertiser_url,
            CASE WHEN date(a.start_date) IS NULL THEN NULL
                 ELSE MAX(1, CAST(julianday('now') - julianday(date(a.start_date))
                                  AS INTEGER) + 1) END      AS age_days,
            MAX(COALESCE(NULLIF(al.final_language,''), NULLIF(t.language,''))) AS language,
            MAX(t.cluster_id)                               AS script_id,
            MAX(t.status)                                   AS transcript_status
        FROM ad_products ap
        JOIN ads a                ON a.id = ap.ad_id
        LEFT JOIN pages p         ON p.id = a.page_id
        LEFT JOIN ad_languages al ON al.ad_id = a.id
        LEFT JOIN transcript_ads ta ON ta.ad_id = a.id
        LEFT JOIN transcripts t   ON t.id = ta.transcript_id
        WHERE ap.product_id = ?
        GROUP BY a.id
        {having}
        ORDER BY (lower(COALESCE(a.status,'active')) = 'active') DESC,
                 date(a.start_date) DESC, a.id DESC
        LIMIT ?
    """
    if language:
        params.append(language)
    params.append(int(limit))
    rows = _rows(sql, params)
    for row in rows:
        row["language_code"] = (row.get("language") or "und").lower()
        row["language_name"] = language_name(row["language_code"])
        row["age_label"] = _age_label(row.get("age_days"))
        row["start_day"] = (row.get("start_date") or "")[:10]
        row["library_url"] = (
            f"https://www.facebook.com/ads/library/?id={row['library_id']}&country=IN"
            if row.get("library_id") else ""
        )
    return rows


def product_languages(product_id: int) -> list[dict]:
    """The language chips above the Linked ads table."""
    rows = _rows(
        """
        SELECT lower(COALESCE(NULLIF(al.final_language,''),
                              NULLIF(t.language,''), 'und')) AS code,
               COUNT(DISTINCT a.id)                          AS ads
        FROM ad_products ap
        JOIN ads a                ON a.id = ap.ad_id
        LEFT JOIN ad_languages al ON al.ad_id = a.id
        LEFT JOIN transcript_ads ta ON ta.ad_id = a.id
        LEFT JOIN transcripts t   ON t.id = ta.transcript_id
        WHERE ap.product_id = ?
        GROUP BY code
        ORDER BY ads DESC, code ASC
        """,
        (int(product_id),),
    )
    for row in rows:
        row["name"] = language_name(row["code"])
    return rows


def product_intel_totals(product_id: int) -> dict:
    """"50 ads are really 5 videos": ads vs unique videos vs unique scripts."""
    row = _row(
        """
        SELECT
            COUNT(DISTINCT a.id)                                   AS ads,
            COUNT(DISTINCT CASE WHEN lower(COALESCE(a.media_type,'')) = 'video'
                                THEN a.id END)                     AS video_ads,
            COUNT(DISTINCT t.id)                                   AS unique_videos,
            COUNT(DISTINCT t.cluster_id)                           AS unique_scripts,
            COUNT(DISTINCT CASE WHEN t.status = 'completed' THEN a.id END) AS transcribed_ads,
            COUNT(DISTINCT CASE WHEN t.status = 'failed'    THEN t.id END) AS failed,
            COUNT(DISTINCT CASE WHEN t.status IN ('pending','processing','claimed')
                                THEN t.id END)                     AS pending
        FROM ad_products ap
        JOIN ads a ON a.id = ap.ad_id
        LEFT JOIN transcript_ads ta ON ta.ad_id = a.id
        LEFT JOIN transcripts t     ON t.id = ta.transcript_id
        WHERE ap.product_id = ?
        """,
        (int(product_id),),
    ) or {}
    totals = {key: int(value or 0) for key, value in row.items()}
    totals["untranscribed_video_ads"] = max(
        0, totals.get("video_ads", 0) - totals.get("transcribed_ads", 0)
    )
    return totals


def product_script_clusters(product_id: int, *, language: str = "") -> list[dict]:
    """Grouped Scripts view: one card per script cluster, newest-heaviest first."""
    params: list[Any] = [int(product_id)]
    where = ["ap.product_id = ?"]
    if language:
        where.append("lower(COALESCE(sc.language,'und')) = ?")
        params.append(language)
    rows = _rows(
        f"""
        SELECT
            sc.id                          AS cluster_id,
            sc.language                    AS language,
            sc.canonical_length            AS canonical_length,
            sc.member_count                AS member_count,
            COUNT(DISTINCT a.id)           AS ad_count,
            COUNT(DISTINCT a.page_id)      AS page_count,
            COUNT(DISTINCT t.id)           AS variant_count,
            MAX(rt.hook_summary)           AS hook,
            MAX(rt.text)                   AS representative
        FROM script_clusters sc
        JOIN transcripts t      ON t.cluster_id = sc.id
        JOIN transcript_ads ta  ON ta.transcript_id = t.id
        JOIN ad_products ap     ON ap.ad_id = ta.ad_id
        JOIN ads a              ON a.id = ta.ad_id
        LEFT JOIN transcripts rt ON rt.id = sc.representative_transcript_id
        WHERE {' AND '.join(where)}
        GROUP BY sc.id
        ORDER BY ad_count DESC, sc.member_count DESC, sc.id ASC
        """,
        params,
    )
    for index, row in enumerate(rows, start=1):
        row["index"] = index
        row["language_code"] = (row.get("language") or "und").lower()
        row["language_name"] = language_name(row["language_code"])
        row["script_label"] = f"S-{row['cluster_id']}"
        row["library_ids"] = _cluster_library_ids(int(row["cluster_id"]), int(product_id))
    return rows


def _cluster_library_ids(cluster_id: int, product_id: int, limit: int = 40) -> list[str]:
    rows = _rows(
        """
        SELECT DISTINCT a.library_id AS library_id
        FROM transcripts t
        JOIN transcript_ads ta ON ta.transcript_id = t.id
        JOIN ad_products ap    ON ap.ad_id = ta.ad_id
        JOIN ads a             ON a.id = ta.ad_id
        WHERE t.cluster_id = ? AND ap.product_id = ? AND a.library_id IS NOT NULL
        ORDER BY a.library_id
        LIMIT ?
        """,
        (cluster_id, product_id, limit),
    )
    return [str(row["library_id"]) for row in rows]


def product_transcripts(product_id: int, *, language: str = "") -> list[dict]:
    """Flat "All transcripts" view."""
    params: list[Any] = [int(product_id)]
    where = ["ap.product_id = ?"]
    if language:
        where.append("lower(COALESCE(t.language,'und')) = ?")
        params.append(language)
    rows = _rows(
        f"""
        SELECT t.id           AS transcript_id,
               t.language     AS language,
               t.hook_summary AS hook,
               t.text         AS transcript,
               t.status       AS status,
               t.cluster_id   AS cluster_id,
               t.low_confidence AS low_confidence,
               MAX(a.library_id) AS library_id,
               COUNT(DISTINCT a.id) AS ad_count
        FROM transcripts t
        JOIN transcript_ads ta ON ta.transcript_id = t.id
        JOIN ad_products ap    ON ap.ad_id = ta.ad_id
        JOIN ads a             ON a.id = ta.ad_id
        WHERE {' AND '.join(where)}
        GROUP BY t.id
        ORDER BY ad_count DESC, t.id ASC
        """,
        params,
    )
    for row in rows:
        row["language_code"] = (row.get("language") or "und").lower()
        row["language_name"] = language_name(row["language_code"])
        row["script_label"] = f"S-{row['cluster_id']}" if row.get("cluster_id") else ""
    return rows


# ---------------------------------------------------------------------------
# products — the grouping view
#
# "Product-wise grouping" is not a second screen: it is the answer to one
# question the owner asks about every product — *where does this thing actually
# run, and how many DIFFERENT creatives is it really?* Three functions answer
# it, and all three are counted from `ads` at request time like everything else
# on this screen.
# ---------------------------------------------------------------------------
def product_reach(product_id: int) -> dict:
    """One row of arithmetic: pages, brand groups, creatives, videos, scripts.

    This is the headline of the drawer, and it is the sentence the tool exists
    to print: *278 ads across 13 pages in 2 brand groups are really 24 videos
    and 5 scripts.* Every number is DISTINCT-counted, so a video reused on nine
    pages counts once as a video and nine times as an ad — which is the whole
    point of the distinction.
    """
    row = _row(
        f"""
        SELECT
            COUNT(DISTINCT a.id)                                    AS ads,
            COUNT(DISTINCT CASE WHEN {_ACTIVE} THEN a.id END)       AS active_ads,
            COUNT(DISTINCT a.page_id)                               AS pages,
            COUNT(DISTINCT gp.group_id)                             AS groups,
            COUNT(DISTINCT CASE WHEN lower(COALESCE(a.media_type,'')) = 'video'
                                THEN a.id END)                      AS video_ads,
            COUNT(DISTINCT t.id)                                    AS unique_videos,
            COUNT(DISTINCT t.cluster_id)                            AS unique_scripts,
            COUNT(DISTINCT CASE WHEN t.status = 'completed' THEN a.id END)
                                                                    AS transcribed_ads
        FROM ad_products ap
        JOIN ads a                  ON a.id = ap.ad_id
        LEFT JOIN group_pages gp    ON gp.page_id = a.page_id
        LEFT JOIN transcript_ads ta ON ta.ad_id = a.id
        LEFT JOIN transcripts t     ON t.id = ta.transcript_id
        WHERE ap.product_id = ?
        """,
        (int(product_id),),
    ) or {}
    reach = {key: int(value or 0) for key, value in row.items()}
    # The compression ratio is the honest version of "50 ads are really 5
    # videos": how many ads one script is carrying. Shown only when there is
    # something to compress, because 1.0x is noise.
    scripts = reach.get("unique_scripts", 0)
    reach["ads_per_script"] = round(reach["ads"] / scripts, 1) if scripts else 0.0
    reach["untranscribed_video_ads"] = max(
        0, reach.get("video_ads", 0) - reach.get("transcribed_ads", 0)
    )
    return reach


def product_groups(product_id: int) -> list[dict]:
    """The brand groups this product runs in, with each group's own share.

    A product that shows up under two brand groups is either one advertiser
    running two storefronts or two advertisers running the same offer — and
    that difference is worth a click, so each row carries the group's page and
    ad counts FOR THIS PRODUCT, never the group's global totals.
    """
    return _rows(
        f"""
        SELECT g.id                                              AS group_id,
               g.name                                            AS group_name,
               COUNT(DISTINCT a.page_id)                         AS pages,
               COUNT(DISTINCT a.id)                              AS ads,
               COUNT(DISTINCT CASE WHEN {_ACTIVE} THEN a.id END) AS active_ads
        FROM ad_products ap
        JOIN ads a          ON a.id = ap.ad_id
        JOIN group_pages gp ON gp.page_id = a.page_id
        JOIN groups g       ON g.id = gp.group_id
        WHERE ap.product_id = ?
        GROUP BY g.id
        ORDER BY active_ads DESC, ads DESC, group_name COLLATE NOCASE ASC
        """,
        (int(product_id),),
    )


def product_page_rollup(product_id: int) -> list[dict]:
    """Per advertiser page: this product's ads there, and how old they are.

    ``attach_advertiser_pages`` gives the list screen its pills; this gives the
    drawer the table, with the two numbers that separate a page that is testing
    a product from a page that is scaling it.
    """
    rows = _rows(
        f"""
        SELECT p.id                                              AS page_id,
               COALESCE(NULLIF(p.alias,''), NULLIF(p.name,''),
                        'Page ' || COALESCE(p.platform_page_id,'?')) AS page_name,
               p.platform_page_id                                AS platform_page_id,
               COUNT(DISTINCT a.id)                              AS ads,
               COUNT(DISTINCT CASE WHEN {_ACTIVE} THEN a.id END) AS active_ads,
               COALESCE(SUM(CASE WHEN {_ACTIVE}
                                 THEN MAX(1, COALESCE(a.represented_ad_count,1))
                                 ELSE 0 END), 0)                 AS represented_ads,
               MIN(date(a.start_date))                           AS oldest_start_date,
               COUNT(DISTINCT t.cluster_id)                      AS scripts
        FROM ad_products ap
        JOIN ads a                  ON a.id = ap.ad_id
        JOIN pages p                ON p.id = a.page_id
        LEFT JOIN transcript_ads ta ON ta.ad_id = a.id
        LEFT JOIN transcripts t     ON t.id = ta.transcript_id
        WHERE ap.product_id = ?
        GROUP BY p.id
        ORDER BY active_ads DESC, ads DESC, page_name COLLATE NOCASE ASC
        """,
        (int(product_id),),
    )
    for row in rows:
        row["oldest_day"] = (row.get("oldest_start_date") or "")[:10]
    return rows


def _growth_pct(current: int, previous: int | None) -> float | None:
    """Growth % of ``current`` vs ``previous``; None when not computable."""
    if previous is None or previous <= 0:
        return None
    return round(100.0 * (current - previous) / previous, 1)


def product_scan_history(product_id: int, limit: int = 12) -> list[dict]:
    """This product's scan history, newest first, with per-scan deltas.

    Each entry aggregates ``product_scan_snapshots`` over the product's pages
    for one scan moment: live ads, new ads, stopped ads — plus ``delta`` and
    ``growth_pct`` vs the previous scan. This is the "18 the, ab 21, kitni
    growth" view. Needs two scanned moments before a delta appears.
    """
    rows = _rows(
        """
        SELECT MAX(scanned_at)                              AS scanned_at,
               SUM(active_ads)                              AS active_ads,
               SUM(new_ads)                                 AS new_ads,
               SUM(stopped_ads)                             AS stopped_ads,
               COUNT(DISTINCT page_id)                      AS pages
        FROM product_scan_snapshots
        WHERE product_id=?
        GROUP BY job_id
        ORDER BY scanned_at DESC, job_id DESC
        LIMIT ?
        """,
        (int(product_id), int(limit)),
    )
    ordered = list(reversed(rows))  # oldest -> newest for delta math
    for prev, cur in zip([None, *ordered], ordered):
        cur_active = int(cur.get("active_ads") or 0)
        if prev is None:
            cur["delta"] = None
            cur["growth_pct"] = None
        else:
            prev_active = int(prev.get("active_ads") or 0)
            cur["delta"] = cur_active - prev_active
            cur["growth_pct"] = _growth_pct(cur_active, prev_active)
        cur["active_ads"] = cur_active
        cur["new_ads"] = int(cur.get("new_ads") or 0)
        cur["stopped_ads"] = int(cur.get("stopped_ads") or 0)
        cur["day"] = (cur.get("scanned_at") or "")[:10]
    return list(reversed(ordered))


def attach_page_rollup_trends(product_id: int, rollup: list[dict]) -> None:
    """Add ``delta``/``growth_pct``/``prev_active`` to each page_rollup row.

    Compares the page's latest snapshot against the one before it, so the
    "Where this product runs" table shows which pages are scaling the product
    and which are winding it down.
    """
    page_ids = [int(r["page_id"]) for r in rollup if r.get("page_id")]
    if not page_ids:
        return
    placeholders = ",".join("?" for _ in page_ids)
    rows = _rows(
        f"""
        SELECT page_id, scanned_at, active_ads
        FROM product_scan_snapshots
        WHERE product_id=? AND page_id IN ({placeholders})
        ORDER BY page_id, scanned_at DESC
        """,
        (int(product_id), *page_ids),
    )
    latest: dict[int, dict] = {}
    previous: dict[int, dict] = {}
    for r in rows:
        pid = int(r["page_id"])
        if pid not in latest:
            latest[pid] = r
        elif pid not in previous:
            previous[pid] = r
    for row in rollup:
        pid = int(row["page_id"])
        cur = latest.get(pid)
        prev = previous.get(pid)
        row["last_scan_day"] = ((cur or {}).get("scanned_at") or "")[:10] or None
        if cur is None or prev is None:
            row["prev_active"] = None
            row["delta"] = None
            row["growth_pct"] = None
        else:
            cur_active = int(cur.get("active_ads") or 0)
            prev_active = int(prev.get("active_ads") or 0)
            row["prev_active"] = prev_active
            row["delta"] = cur_active - prev_active
            row["growth_pct"] = _growth_pct(cur_active, prev_active)


def attach_product_trends(items: list[dict]) -> None:
    """Add ``trend_delta``/``trend_growth_pct`` to product list rows.

    One batched query over the latest two snapshots per product — the list
    stays one query, not N. Products scanned fewer than twice get None.
    """
    ids = [int(item["id"]) for item in items if item.get("id")]
    for item in items:
        item["trend_delta"] = None
        item["trend_growth_pct"] = None
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    rows = _rows(
        f"""
        SELECT product_id, MAX(scanned_at) AS scanned_at, SUM(active_ads) AS active_ads
        FROM product_scan_snapshots
        WHERE product_id IN ({placeholders})
        GROUP BY product_id, job_id
        ORDER BY product_id, scanned_at DESC, job_id DESC
        """,
        ids,
    )
    seen: dict[int, list[int]] = {}
    for r in rows:
        seen.setdefault(int(r["product_id"]), []).append(int(r["active_ads"] or 0))
    by_id = {int(item["id"]): item for item in items if item.get("id")}
    for pid, series in seen.items():
        if len(series) >= 2 and pid in by_id:
            by_id[pid]["trend_delta"] = series[0] - series[1]
            by_id[pid]["trend_growth_pct"] = _growth_pct(series[0], series[1])


def product_ad_cards(product_id: int, *, language: str = "", limit: int = 60) -> list[dict]:
    """The same ads as ``product_ads``, shaped for the shared ad-card grid.

    The drawer's default view is the table of columns v1 had, because that is
    what the owner sorts and exports. But "what does this product's creative
    actually look like" is a picture question, and _drawer.html already owns the
    only ad tile in the app — so this fills that macro's fields rather than
    inventing a second card.
    """
    params: list[Any] = [int(product_id)]
    having = ""
    if language:
        having = "HAVING lower(COALESCE(language,'und')) = ?"
    sql = f"""
        SELECT a.id                       AS id,
               a.library_id               AS library_id,
               a.status                   AS status,
               a.start_date               AS start_date,
               a.headline                 AS headline,
               a.ad_text                  AS ad_text,
               a.destination_url          AS destination_url,
               a.media_type               AS media_type,
               a.media_urls               AS media_urls,
               a.represented_ad_count     AS represented_ad_count,
               (SELECT COUNT(*) FROM ad_versions av WHERE av.ad_id = a.id) AS version_count,
               MAX(COALESCE(NULLIF(al.final_language,''), NULLIF(t.language,''))) AS language,
               MAX(t.cluster_id)          AS script_id,
               MAX(t.hook_summary)        AS hook,
               CASE WHEN date(a.start_date) IS NULL THEN NULL
                    ELSE MAX(1, CAST(julianday('now') - julianday(date(a.start_date))
                                     AS INTEGER) + 1) END        AS days_running
        FROM ad_products ap
        JOIN ads a                  ON a.id = ap.ad_id
        LEFT JOIN ad_languages al   ON al.ad_id = a.id
        LEFT JOIN transcript_ads ta ON ta.ad_id = a.id
        LEFT JOIN transcripts t     ON t.id = ta.transcript_id
        WHERE ap.product_id = ?
        GROUP BY a.id
        {having}
        ORDER BY (lower(COALESCE(a.status,'active')) = 'active') DESC,
                 date(a.start_date) DESC, a.id DESC
        LIMIT ?
    """
    if language:
        params.append(language)
    params.append(int(limit))
    cards = []
    for row in _rows(sql, params):
        media = _json_list(row.get("media_urls"))
        text = (row.get("ad_text") or "").strip()
        cards.append({
            **row,
            "media_list": media,
            "media_count": len(media),
            "preview_url": media[0] if media else "",
            "is_video": (row.get("media_type") or "").lower() == "video",
            "snippet": text[:220] + ("…" if len(text) > 220 else ""),
            "library_url": (
                f"https://www.facebook.com/ads/library/?id={row['library_id']}&country=IN"
                if row.get("library_id") else ""
            ),
            "script_label": f"S-{row['script_id']}" if row.get("script_id") else "",
            "language_name": language_name((row.get("language") or "und").lower()),
        })
    return cards


def _json_list(raw: Any) -> list[str]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item or "").strip()]


def transcription_state(product_id: int) -> dict:
    """What "Generate scripts" would actually do, before it is pressed.

    Nothing here starts anything — it is the count the confirm dialog quotes, so
    the owner knows the size of the bill before agreeing to it.
    """
    row = _row(
        """
        SELECT
            COUNT(DISTINCT CASE WHEN lower(COALESCE(a.media_type,'unknown'))
                                     IN ('video','unknown','') THEN a.id END) AS candidate_ads,
            COUNT(DISTINCT CASE WHEN t.status = 'completed'  THEN t.id END)   AS done,
            COUNT(DISTINCT CASE WHEN t.status = 'pending'    THEN t.id END)   AS pending,
            COUNT(DISTINCT CASE WHEN t.status = 'processing' THEN t.id END)   AS processing,
            COUNT(DISTINCT CASE WHEN t.status = 'failed'     THEN t.id END)   AS failed,
            COUNT(DISTINCT CASE WHEN COALESCE(t.low_confidence,0) = 1
                                THEN t.id END)                                AS guarded
        FROM ad_products ap
        JOIN ads a                  ON a.id = ap.ad_id
        LEFT JOIN transcript_ads ta ON ta.ad_id = a.id
        LEFT JOIN transcripts t     ON t.id = ta.transcript_id
        WHERE ap.product_id = ?
        """,
        (int(product_id),),
    ) or {}
    state = {key: int(value or 0) for key, value in row.items()}
    # An ad with no stored media URL cannot be transcribed at all. Today that is
    # almost all of them (v2's extractor captures no media — docs/09 Group C),
    # so saying it plainly beats a run that queues nothing and looks broken.
    missing = _row(
        """
        SELECT COUNT(DISTINCT a.id) AS n
        FROM ad_products ap
        JOIN ads a ON a.id = ap.ad_id
        WHERE ap.product_id = ?
          AND lower(COALESCE(a.media_type,'unknown')) IN ('video','unknown','')
          AND COALESCE(NULLIF(a.media_urls,''), '[]') IN ('[]','')
        """,
        (int(product_id),),
    ) or {}
    state["no_media"] = int(missing.get("n") or 0)
    state["transcribable"] = max(0, state["candidate_ads"] - state["no_media"])
    return state


def product_language_queue(product_id: int) -> list[dict]:
    """Per-language counts for the transcription language filter.

    The owner does not always want every language at once — "just the Marathi
    ones" is a real request and a real cost saving — so the picker is populated
    from the ads' OWN detected language, not from transcripts that do not exist
    yet.
    """
    # ad_languages first (detected from the ad's own copy), then the language of
    # a transcript the ad already has. Without that second source every row on a
    # v1-imported product reads "Unknown", because ad_languages has 0 rows and
    # every language the owner can see today came in on a transcript.
    rows = _rows(
        """
        SELECT lower(COALESCE(NULLIF(al.final_language,''),
                              NULLIF(al.text_language,''),
                              NULLIF(MAX(t.language),''), 'und')) AS code,
               COUNT(DISTINCT a.id)                                AS ads
        FROM ad_products ap
        JOIN ads a                  ON a.id = ap.ad_id
        LEFT JOIN ad_languages al   ON al.ad_id = a.id
        LEFT JOIN transcript_ads ta ON ta.ad_id = a.id
        LEFT JOIN transcripts t     ON t.id = ta.transcript_id
        WHERE ap.product_id = ?
          AND lower(COALESCE(a.media_type,'unknown')) IN ('video','unknown','')
        GROUP BY a.id
        """,
        (int(product_id),),
    )
    tally: dict[str, int] = {}
    for row in rows:
        tally[row["code"]] = tally.get(row["code"], 0) + 1
    rows = [{"code": code, "ads": ads} for code, ads in
            sorted(tally.items(), key=lambda item: (-item[1], item[0]))]
    for row in rows:
        row["name"] = language_name(row["code"])
        row["filter_value"] = "unknown" if row["code"] == "und" else row["code"]
    return rows


def script_language_options(product_id: int) -> list[dict]:
    """Languages that actually have transcripts for this product."""
    rows = _rows(
        """
        SELECT lower(COALESCE(t.language,'und')) AS code,
               COUNT(DISTINCT t.id)              AS transcripts,
               COUNT(DISTINCT t.cluster_id)      AS scripts
        FROM transcripts t
        JOIN transcript_ads ta ON ta.transcript_id = t.id
        JOIN ad_products ap    ON ap.ad_id = ta.ad_id
        WHERE ap.product_id = ?
        GROUP BY code
        ORDER BY transcripts DESC, code ASC
        """,
        (int(product_id),),
    )
    for row in rows:
        row["name"] = language_name(row["code"])
    return rows


def ad_column_value(ad: dict, key: str) -> str:
    """One cell, as text. Shared by the table and the CSV export so a column
    never says one thing on screen and another in the file."""
    if key == "ad_id":
        return str(ad.get("ad_id") or "")
    if key == "library_id":
        return str(ad.get("library_id") or "")
    if key == "ad_library_url":
        return str(ad.get("library_url") or "")
    if key == "meta_page_id":
        return str(ad.get("platform_page_id") or "")
    if key == "advertiser":
        return str(ad.get("page_name") or ad.get("platform_page_id") or "")
    if key == "advertiser_url":
        return str(ad.get("advertiser_url") or "")
    if key == "status":
        return str(ad.get("status") or "")
    if key == "age":
        return str(ad.get("age_label") or "")
    if key == "format":
        return str(ad.get("media_type") or "")
    if key == "language":
        return str(ad.get("language_name") or "") if ad.get("language") else ""
    if key == "script_id":
        return f"S-{ad['script_id']}" if ad.get("script_id") else ""
    if key == "start_date":
        return str(ad.get("start_day") or "")
    return ""


# ---------------------------------------------------------------------------
# products — writes
# ---------------------------------------------------------------------------
def set_product_state(product_id: int, field: str, on: bool) -> bool:
    if field not in PRODUCT_STATE_FIELDS:
        return False
    now = utc_now()
    with db.transaction():
        db.execute(
            "INSERT INTO product_states (product_id, updated_at) VALUES (?, ?)"
            " ON CONFLICT(product_id) DO NOTHING",
            (int(product_id), now),
        )
        db.execute(
            f"UPDATE product_states SET {field} = ?, {field}_at = ?, updated_at = ?"
            " WHERE product_id = ?",
            (1 if on else 0, now if on else None, now, int(product_id)),
        )
    return True


def remove_product(product_id: int) -> str:
    """v1's "Remove permanently": drop the product and remember it was removed
    so the next extraction run does not resurrect it."""
    product = _row(
        "SELECT id, display_name, normalized_name, product_url, domain"
        " FROM products WHERE id = ?",
        (int(product_id),),
    )
    if product is None:
        return ""
    now = utc_now()
    with db.transaction():
        db.execute(
            "INSERT INTO product_removed (identity_hash, normalized_name, canonical_url,"
            " domain, display_name, removed_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(identity_hash) DO UPDATE SET removed_at = excluded.removed_at",
            (
                f"product:{product['id']}",
                product.get("normalized_name"),
                product.get("product_url"),
                product.get("domain"),
                product.get("display_name"),
                now,
            ),
        )
        db.execute("DELETE FROM ad_products WHERE product_id = ?", (int(product_id),))
        db.execute("DELETE FROM product_states WHERE product_id = ?", (int(product_id),))
        db.execute("DELETE FROM product_meta WHERE product_id = ?", (int(product_id),))
        db.execute("DELETE FROM products WHERE id = ?", (int(product_id),))
    return str(product.get("display_name") or "product")


# ---------------------------------------------------------------------------
# products — the advertiser-page picker and page sets
# ---------------------------------------------------------------------------
def advertiser_pages(
    *, search: str = "", page: int = 1, per_page: int = 60, saved_only: bool = False
) -> dict:
    where = ["p.is_hidden = 0"]
    params: list[Any] = []
    needle = clean_text(search).lower()
    if needle:
        like = f"%{needle}%"
        where.append(
            "(lower(COALESCE(p.name,'')) LIKE ? OR lower(COALESCE(p.alias,'')) LIKE ?"
            " OR lower(COALESCE(p.platform_page_id,'')) LIKE ?)"
        )
        params += [like, like, like]
    if saved_only:
        where.append("COALESCE(ps.is_saved, 0) = 1")

    base = f"""
        FROM pages p
        LEFT JOIN page_states ps ON ps.page_id = p.id
        WHERE {' AND '.join(where)}
    """
    total = int((_row(f"SELECT COUNT(*) AS n {base}", params) or {}).get("n") or 0)
    meta = _paginate(total, positive_int(page, 1), max(1, int(per_page)))
    rows = _rows(
        f"""
        SELECT p.id                                             AS id,
               p.platform_page_id                               AS platform_page_id,
               COALESCE(NULLIF(p.alias,''), NULLIF(p.name,''),
                        'Page ' || COALESCE(p.platform_page_id,'?')) AS page_name,
               COALESCE(ps.is_saved, 0)                         AS is_saved,
               (SELECT COUNT(DISTINCT a.id) FROM ads a
                 WHERE a.page_id = p.id
                   AND lower(COALESCE(a.status,'active')) = 'active') AS active_ads,
               (SELECT COUNT(DISTINCT ap.product_id)
                  FROM ad_products ap JOIN ads a2 ON a2.id = ap.ad_id
                 WHERE a2.page_id = p.id)                       AS product_count
        {base}
        ORDER BY active_ads DESC, page_name COLLATE NOCASE ASC
        LIMIT ? OFFSET ?
        """,
        [*params, meta["per_page"], meta["offset"]],
    )
    for row in rows:
        row["is_saved"] = bool(row.get("is_saved"))
    return {"items": rows, "search": clean_text(search), **meta,
            "end": min(meta["offset"] + len(rows), total)}


def pages_by_id(page_ids: Sequence[int]) -> list[dict]:
    ids = list(page_ids)
    if not ids:
        return []
    return _rows(
        f"""
        SELECT p.id AS id, p.platform_page_id AS platform_page_id,
               COALESCE(NULLIF(p.alias,''), NULLIF(p.name,''),
                        'Page ' || COALESCE(p.platform_page_id,'?')) AS page_name
        FROM pages p WHERE p.id IN ({_placeholders(ids)})
        ORDER BY page_name COLLATE NOCASE ASC
        """,
        ids,
    )


def set_page_saved(page_ids: Sequence[int], saved: bool) -> int:
    ids = list(page_ids)
    if not ids:
        return 0
    now = utc_now()
    with db.transaction():
        for page_id in ids:
            db.execute(
                "INSERT INTO page_states (page_id, updated_at) VALUES (?, ?)"
                " ON CONFLICT(page_id) DO NOTHING",
                (int(page_id), now),
            )
            db.execute(
                "UPDATE page_states SET is_saved = ?, saved_at = ?, updated_at = ?"
                " WHERE page_id = ?",
                (1 if saved else 0, now if saved else None, now, int(page_id)),
            )
    return len(ids)


def list_page_sets(search: str = "") -> list[dict]:
    where = ["1 = 1"]
    params: list[Any] = []
    needle = clean_text(search).lower()
    if needle:
        where.append("lower(s.name) LIKE ?")
        params.append(f"%{needle}%")
    rows = _rows(
        f"""
        SELECT s.id AS id, s.name AS name, s.created_at AS created_at,
               s.updated_at AS updated_at,
               COUNT(i.page_id) AS page_count
        FROM product_page_sets s
        LEFT JOIN product_page_set_items i ON i.set_id = s.id
        WHERE {' AND '.join(where)}
        GROUP BY s.id
        ORDER BY COALESCE(s.updated_at, s.created_at) DESC, s.name COLLATE NOCASE ASC
        """,
        params,
    )
    for row in rows:
        row["page_ids"] = [
            int(item["page_id"])
            for item in _rows(
                "SELECT page_id FROM product_page_set_items"
                " WHERE set_id = ? ORDER BY sort_order, page_id",
                (int(row["id"]),),
            )
        ]
        names = pages_by_id(row["page_ids"][:6])
        row["preview"] = ", ".join(item["page_name"] for item in names) or "No pages"
    return rows


def create_page_set(name: str, page_ids: Sequence[int]) -> int:
    label = clean_text(name, 80) or "Untitled set"
    now = utc_now()
    with db.transaction():
        cursor = db.execute(
            "INSERT INTO product_page_sets (name, created_at, updated_at) VALUES (?, ?, ?)",
            (label, now, now),
        )
        set_id = int(cursor.lastrowid)
        for order, page_id in enumerate(page_ids):
            db.execute(
                "INSERT INTO product_page_set_items (set_id, page_id, sort_order, added_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT(set_id, page_id) DO NOTHING",
                (set_id, int(page_id), order, now),
            )
    return set_id


def delete_page_set(set_id: int) -> None:
    with db.transaction():
        db.execute("DELETE FROM product_page_set_items WHERE set_id = ?", (int(set_id),))
        db.execute("DELETE FROM product_page_sets WHERE id = ?", (int(set_id),))


# ---------------------------------------------------------------------------
# brand groups — list and detail
# ---------------------------------------------------------------------------
def list_groups(*, sort: str = DEFAULT_GROUP_SORT, search: str = "") -> list[dict]:
    sort = sort if sort in GROUP_SORTS else DEFAULT_GROUP_SORT
    where = ["1 = 1"]
    params: list[Any] = []
    needle = clean_text(search).lower()
    if needle:
        where.append("(lower(g.name) LIKE ? OR lower(COALESCE(m.primary_domain,'')) LIKE ?)")
        params += [f"%{needle}%", f"%{needle}%"]
    rows = _rows(
        f"""
        SELECT g.id                                   AS id,
               g.name                                 AS name,
               g.notes                                AS notes,
               g.created_at                           AS created_at,
               g.updated_at                           AS updated_at,
               m.primary_domain                       AS primary_domain,
               m.category                             AS category,
               COUNT(DISTINCT gp.page_id)             AS page_count,
               COUNT(DISTINCT CASE WHEN {_ACTIVE} THEN a.id END) AS live_ads,
               COUNT(DISTINCT ap.product_id)          AS product_count
        FROM "groups" g
        LEFT JOIN group_meta m   ON m.group_id = g.id
        LEFT JOIN group_pages gp ON gp.group_id = g.id
        LEFT JOIN ads a          ON a.page_id = gp.page_id
        LEFT JOIN ad_products ap ON ap.ad_id = a.id
        WHERE {' AND '.join(where)}
        GROUP BY g.id
        ORDER BY {GROUP_SORTS[sort]}
        """,
        params,
    )
    for row in rows:
        row["members"] = _group_member_names(int(row["id"]), limit=4)
    return rows


def _group_member_names(group_id: int, limit: int = 4) -> list[str]:
    rows = _rows(
        """
        SELECT COALESCE(NULLIF(p.alias,''), NULLIF(p.name,''),
                        'Page ' || COALESCE(p.platform_page_id,'?')) AS page_name
        FROM group_pages gp JOIN pages p ON p.id = gp.page_id
        WHERE gp.group_id = ?
        ORDER BY p.active_ads DESC, page_name COLLATE NOCASE ASC
        LIMIT ?
        """,
        (int(group_id), int(limit)),
    )
    return [row["page_name"] for row in rows]


def get_group(group_id: int) -> dict | None:
    return _row(
        """
        SELECT g.id AS id, g.name AS name, g.notes AS notes,
               g.created_at AS created_at, g.updated_at AS updated_at,
               m.primary_domain AS primary_domain, m.category AS category,
               m.color_key AS color_key
        FROM "groups" g LEFT JOIN group_meta m ON m.group_id = g.id
        WHERE g.id = ?
        """,
        (int(group_id),),
    )


def group_summary(group_id: int) -> dict:
    """The six KPI tiles, in v1's order (Pages, Active, Represented, Products,
    Video ads, Oldest days)."""
    row = _row(
        f"""
        SELECT
            (SELECT COUNT(*) FROM group_pages WHERE group_id = ?)   AS page_count,
            COUNT(DISTINCT CASE WHEN {_ACTIVE} THEN a.id END)       AS live_ads,
            COALESCE(SUM(CASE WHEN {_ACTIVE}
                              THEN MAX(1, COALESCE(a.represented_ad_count,1))
                              ELSE 0 END), 0)                       AS represented_ads,
            COUNT(DISTINCT ap.product_id)                           AS unique_products,
            COUNT(DISTINCT CASE WHEN lower(COALESCE(a.media_type,'')) = 'video'
                                THEN a.id END)                      AS video_ads,
            CASE WHEN MIN(date(a.start_date)) IS NULL THEN 0
                 ELSE CAST(julianday('now') - julianday(MIN(date(a.start_date)))
                           AS INTEGER) END                          AS oldest_days
        FROM group_pages gp
        LEFT JOIN ads a          ON a.page_id = gp.page_id
        LEFT JOIN ad_products ap ON ap.ad_id = a.id
        WHERE gp.group_id = ?
        """,
        (int(group_id), int(group_id)),
    ) or {}
    return {key: int(value or 0) for key, value in row.items()}


def group_pages(
    *,
    group_id: int,
    category: str = "all",
    order: str = "live_desc",
    search: str = "",
    page: int = 1,
    per_page: int = GROUP_PAGE_SIZE,
) -> dict:
    """The Pages tab. Columns, in v1's order:
    Page | Sources | FB results | Ads scraped | Boxes | Products | Oldest |
    Top product | Actions."""
    rows = _rows(
        f"""
        SELECT
            p.id                                              AS id,
            p.platform_page_id                                AS platform_page_id,
            COALESCE(NULLIF(p.alias,''), NULLIF(p.name,''),
                     'Page ' || COALESCE(p.platform_page_id,'?')) AS page_name,
            p.url                                             AS url,
            p.is_tracked                                      AS is_tracked,
            p.fb_estimated_results                            AS fb_results,
            p.last_verified_at                                AS fb_results_captured_at,
            CASE WHEN p.last_verified_at IS NULL THEN NULL
                 ELSE CAST(julianday('now') - julianday(p.last_verified_at)
                           AS INTEGER) END                    AS fb_results_age_days,
            COALESCE(i.logical_key, 'page:' || p.platform_page_id) AS logical_key,
            (SELECT COUNT(*) FROM page_identity i2
              WHERE i2.logical_key = COALESCE(i.logical_key,
                                              'page:' || p.platform_page_id)) AS identity_sources,
            COUNT(DISTINCT CASE WHEN {_ACTIVE} THEN a.id END) AS live_ads,
            COALESCE(SUM(CASE WHEN {_ACTIVE}
                              THEN MAX(1, COALESCE(a.represented_ad_count,1))
                              ELSE 0 END), 0)                 AS represented_ads,
            COUNT(DISTINCT CASE WHEN lower(COALESCE(a.media_type,'')) = 'video'
                                THEN a.id END)                AS video_ads,
            COUNT(DISTINCT CASE WHEN lower(COALESCE(a.media_type,'')) = 'image'
                                THEN a.id END)                AS image_ads,
            COUNT(DISTINCT ap.product_id)                     AS unique_products,
            CASE WHEN MIN(date(a.start_date)) IS NULL THEN 0
                 ELSE CAST(julianday('now') - julianday(MIN(date(a.start_date)))
                           AS INTEGER) END                    AS oldest_days
        FROM group_pages gp
        JOIN pages p             ON p.id = gp.page_id
        LEFT JOIN page_identity i ON i.page_id = p.id
        LEFT JOIN ads a          ON a.page_id = p.id
        LEFT JOIN ad_products ap ON ap.ad_id = a.id
        WHERE gp.group_id = ?
        GROUP BY p.id
        """,
        (int(group_id),),
    )

    for row in rows:
        row["source_page_count"] = max(1, int(row.get("identity_sources") or 0) or 1)
        row["is_tracked"] = bool(row.get("is_tracked"))
        row["ads_scraped"] = int(row.get("represented_ads") or 0)
        row["ad_boxes"] = int(row.get("live_ads") or 0)
        row["top_product"] = _top_product_for_page(int(row["id"]))
        row["library_url"] = (
            f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
            f"&country=IN&view_all_page_id={row['platform_page_id']}"
            if row.get("platform_page_id") else ""
        )

    needle = clean_text(search).lower()
    if needle:
        rows = [
            row for row in rows
            if needle in " ".join([
                str(row.get("page_name") or ""),
                str(row.get("platform_page_id") or ""),
                str(row.get("top_product") or ""),
            ]).lower()
        ]

    # v1 tabs/brand_group/queries.py:2931-2943 — same thresholds, same names.
    category = category if category in dict(GROUP_PAGE_CATEGORIES) else "all"
    if category == "multiple":
        rows = [r for r in rows if r["source_page_count"] > 1]
    elif category == "single":
        rows = [r for r in rows if r["source_page_count"] == 1]
    elif category == "evergreen":
        rows = [r for r in rows if int(r["oldest_days"]) >= 120]
    elif category == "product_heavy":
        rows = [r for r in rows if int(r["unique_products"]) >= 10]
    elif category == "video":
        rows = [r for r in rows if int(r["video_ads"]) > int(r["image_ads"])]
    elif category == "weak":
        rows = [r for r in rows if int(r["live_ads"]) <= 2]

    order = order if order in dict(GROUP_PAGE_ORDERS) else "live_desc"
    sorters = {
        "live_desc": lambda r: (-int(r["live_ads"]), str(r["page_name"]).casefold()),
        "represented_desc": lambda r: (-int(r["represented_ads"]), -int(r["live_ads"])),
        "products_desc": lambda r: (-int(r["unique_products"]), -int(r["live_ads"])),
        "oldest_desc": lambda r: (-int(r["oldest_days"]), -int(r["live_ads"])),
        "sources_desc": lambda r: (-int(r["source_page_count"]), -int(r["live_ads"])),
        "name_asc": lambda r: (str(r["page_name"]).casefold(),),
    }
    rows.sort(key=sorters[order])

    meta = _paginate(len(rows), positive_int(page, 1), max(1, int(per_page)))
    window = rows[meta["offset"]: meta["offset"] + meta["per_page"]]
    return {"items": window, "category": category, "order": order,
            "search": clean_text(search), **meta,
            "end": min(meta["offset"] + len(window), meta["total"])}


def _top_product_for_page(page_id: int) -> str:
    row = _row(
        f"""
        SELECT pr.display_name AS name,
               COUNT(DISTINCT CASE WHEN {_ACTIVE} THEN a.id END) AS active_ads
        FROM ad_products ap
        JOIN ads a      ON a.id = ap.ad_id
        JOIN products pr ON pr.id = ap.product_id
        WHERE a.page_id = ?
        GROUP BY pr.id
        ORDER BY active_ads DESC, pr.display_name COLLATE NOCASE ASC
        LIMIT 1
        """,
        (int(page_id),),
    )
    return str((row or {}).get("name") or "")


def group_products(
    *,
    group_id: int,
    visibility: str = "visible",
    sort: str = "active_desc",
    age_bucket: str = "all",
    min_active: int | None = None,
    min_represented: int | None = None,
    min_pages: int | None = None,
    search: str = "",
    page: int = 1,
    per_page: int = GROUP_PAGE_SIZE,
) -> dict:
    """The Products tab. Columns, in v1's order:
    Product | Domain | Active | Represented | Pages | Age | Video | Image | Actions."""
    params: list[Any] = [int(group_id)]
    where = ["gp.group_id = ?"]
    needle = clean_text(search).lower()
    if needle:
        like = f"%{needle}%"
        where.append(
            "(lower(COALESCE(pr.display_name,'')) LIKE ?"
            " OR lower(COALESCE(pr.product_url,'')) LIKE ?"
            " OR lower(COALESCE(pr.domain,'')) LIKE ?)"
        )
        params += [like, like, like]

    rows = _rows(
        f"""
        SELECT
            pr.id                                             AS id,
            pr.display_name                                   AS product_name,
            pr.domain                                         AS store_domain,
            COALESCE(NULLIF(pr.product_url,''),
                     MAX(NULLIF(a.destination_url,'')))       AS product_link,
            COUNT(DISTINCT CASE WHEN {_ACTIVE} THEN a.id END) AS active_ads,
            COALESCE(SUM(CASE WHEN {_ACTIVE}
                              THEN MAX(1, COALESCE(a.represented_ad_count,1))
                              ELSE 0 END), 0)                 AS represented_ads,
            COUNT(DISTINCT a.page_id)                         AS page_count,
            COUNT(DISTINCT CASE WHEN lower(COALESCE(a.media_type,'')) = 'video'
                                THEN a.id END)                AS video_ads,
            COUNT(DISTINCT CASE WHEN lower(COALESCE(a.media_type,'')) = 'image'
                                THEN a.id END)                AS image_ads,
            CASE WHEN MIN(date(a.start_date)) IS NULL THEN NULL
                 ELSE MAX(1, CAST(julianday('now') - julianday(MIN(date(a.start_date)))
                                  AS INTEGER) + 1) END        AS age_days,
            CASE WHEN MAX(date(a.start_date)) IS NULL THEN NULL
                 ELSE MAX(1, CAST(julianday('now') - julianday(MAX(date(a.start_date)))
                                  AS INTEGER) + 1) END        AS newest_age_days,
            COALESCE(st.tracked, 0)                           AS is_tracked,
            COALESCE(st.hidden, 0)                            AS is_hidden
        FROM group_pages gp
        JOIN ads a           ON a.page_id = gp.page_id
        JOIN ad_products ap  ON ap.ad_id = a.id
        JOIN products pr     ON pr.id = ap.product_id
        LEFT JOIN product_states st ON st.product_id = pr.id
        WHERE {' AND '.join(where)}
        GROUP BY pr.id
        """,
        params,
    )

    visibility = visibility if visibility in dict(GROUP_PRODUCT_VISIBILITY) else "visible"
    want_hidden = visibility == "hidden"
    rows = [r for r in rows if bool(r.get("is_hidden")) == want_hidden]

    if min_active is not None:
        rows = [r for r in rows if int(r["active_ads"]) >= min_active]
    if min_represented is not None:
        rows = [r for r in rows if int(r["represented_ads"]) >= min_represented]
    if min_pages is not None:
        rows = [r for r in rows if int(r["page_count"]) >= min_pages]

    bands = {
        "age_0_30": (0, 30), "age_31_90": (31, 90), "age_91_180": (91, 180),
        "age_181_365": (181, 365), "age_365_plus": (365, 10 ** 9),
    }
    age_bucket = age_bucket if age_bucket in dict(GROUP_PRODUCT_AGES) else "all"
    if age_bucket == "unknown":
        rows = [r for r in rows if r.get("age_days") is None]
    elif age_bucket in bands:
        low, high = bands[age_bucket]
        rows = [r for r in rows
                if r.get("age_days") is not None and low <= int(r["age_days"]) <= high]

    sort = sort if sort in dict(GROUP_PRODUCT_SORTS) else "active_desc"
    sorters = {
        "active_desc": lambda r: (-int(r["active_ads"]), -int(r["represented_ads"])),
        "represented_desc": lambda r: (-int(r["represented_ads"]), -int(r["active_ads"])),
        "pages_desc": lambda r: (-int(r["page_count"]), -int(r["active_ads"])),
        "age_oldest": lambda r: (r.get("age_days") is None, -int(r.get("age_days") or 0)),
        "age_newest": lambda r: (r.get("newest_age_days") is None,
                                 int(r.get("newest_age_days") or 10 ** 9)),
        "video_desc": lambda r: (-int(r["video_ads"]), -int(r["active_ads"])),
        "image_desc": lambda r: (-int(r["image_ads"]), -int(r["active_ads"])),
        "name_asc": lambda r: (str(r["product_name"] or "").casefold(),),
    }
    rows.sort(key=sorters[sort])

    for row in rows:
        row["product_name"] = (row.get("product_name") or "").strip() or "Unnamed product"
        row["age_label"] = _age_label(row.get("age_days"))
        row["is_tracked"] = bool(row.get("is_tracked"))
        row["is_hidden"] = bool(row.get("is_hidden"))

    meta = _paginate(len(rows), positive_int(page, 1), max(1, int(per_page)))
    window = rows[meta["offset"]: meta["offset"] + meta["per_page"]]
    return {"items": window, "visibility": visibility, "sort": sort,
            "age_bucket": age_bucket, "search": clean_text(search),
            "min_active": min_active, "min_represented": min_represented,
            "min_pages": min_pages, **meta,
            "end": min(meta["offset"] + len(window), meta["total"])}


def group_page_names(group_id: int) -> list[str]:
    rows = _rows(
        """
        SELECT COALESCE(NULLIF(p.alias,''), NULLIF(p.name,''),
                        'Page ' || COALESCE(p.platform_page_id,'?')) AS page_name
        FROM group_pages gp JOIN pages p ON p.id = gp.page_id
        WHERE gp.group_id = ?
        ORDER BY page_name COLLATE NOCASE ASC
        """,
        (int(group_id),),
    )
    return [row["page_name"] for row in rows]


def group_picker_pages(
    *, group_id: int | None, scope: str = "ungrouped", search: str = "",
    sort: str = "active_desc", limit: int = 200,
) -> list[dict]:
    """The "Add pages" list inside the group modal."""
    scope = scope if scope in dict(GROUP_PAGE_SCOPES) else "ungrouped"
    sort = sort if sort in dict(GROUP_PICKER_SORTS) else "active_desc"
    where = ["p.is_hidden = 0"]
    params: list[Any] = []

    if scope == "ungrouped":
        where.append("NOT EXISTS (SELECT 1 FROM group_pages gp WHERE gp.page_id = p.id)")
    elif scope == "current" and group_id:
        where.append("EXISTS (SELECT 1 FROM group_pages gp"
                     " WHERE gp.page_id = p.id AND gp.group_id = ?)")
        params.append(int(group_id))

    needle = clean_text(search).lower()
    if needle:
        like = f"%{needle}%"
        where.append(
            "(lower(COALESCE(p.name,'')) LIKE ? OR lower(COALESCE(p.alias,'')) LIKE ?"
            " OR lower(COALESCE(p.platform_page_id,'')) LIKE ?)"
        )
        params += [like, like, like]

    order = {
        "active_desc": "active_ads DESC, page_name COLLATE NOCASE ASC",
        "represented_desc": "represented_ads DESC, page_name COLLATE NOCASE ASC",
        "name_asc": "page_name COLLATE NOCASE ASC",
    }[sort]

    rows = _rows(
        f"""
        SELECT p.id AS id, p.platform_page_id AS platform_page_id,
               COALESCE(NULLIF(p.alias,''), NULLIF(p.name,''),
                        'Page ' || COALESCE(p.platform_page_id,'?')) AS page_name,
               COALESCE(p.active_ads, 0)      AS active_ads,
               COALESCE(p.represented_ads, 0) AS represented_ads,
               EXISTS (SELECT 1 FROM group_pages gp WHERE gp.page_id = p.id
                        AND gp.group_id = COALESCE(?, -1))  AS in_group
        FROM pages p
        WHERE {' AND '.join(where)}
        ORDER BY {order}
        LIMIT ?
        """,
        [int(group_id) if group_id else None, *params, int(limit)],
    )
    for row in rows:
        row["in_group"] = bool(row.get("in_group"))
    return rows


# ---------------------------------------------------------------------------
# brand groups — writes
# ---------------------------------------------------------------------------
def create_group(*, name: str, primary_domain: str = "", category: str = "",
                 notes: str = "") -> int:
    label = clean_text(name, 120) or "Untitled group"
    now = utc_now()
    with db.transaction():
        cursor = db.execute(
            'INSERT INTO "groups" (name, notes, created_at, updated_at) VALUES (?, ?, ?, ?)',
            (label, clean_text(notes, 4000), now, now),
        )
        group_id = int(cursor.lastrowid)
        _write_group_meta(group_id, primary_domain, category, now)
    return group_id


def update_group(group_id: int, *, name: str, primary_domain: str = "",
                 category: str = "", notes: str = "") -> None:
    now = utc_now()
    with db.transaction():
        db.execute(
            'UPDATE "groups" SET name = ?, notes = ?, updated_at = ? WHERE id = ?',
            (clean_text(name, 120) or "Untitled group", clean_text(notes, 4000),
             now, int(group_id)),
        )
        _write_group_meta(int(group_id), primary_domain, category, now)


def _write_group_meta(group_id: int, primary_domain: str, category: str, now: str) -> None:
    db.execute(
        "INSERT INTO group_meta (group_id, primary_domain, category, updated_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(group_id) DO UPDATE SET primary_domain = excluded.primary_domain,"
        " category = excluded.category, updated_at = excluded.updated_at",
        (group_id, clean_text(primary_domain, 200) or None,
         clean_text(category, 120) or None, now),
    )


def delete_group(group_id: int) -> str:
    group = get_group(group_id)
    if group is None:
        return ""
    with db.transaction():
        db.execute("DELETE FROM group_pages WHERE group_id = ?", (int(group_id),))
        db.execute("DELETE FROM group_page_meta WHERE group_id = ?", (int(group_id),))
        db.execute("DELETE FROM group_meta WHERE group_id = ?", (int(group_id),))
        db.execute('DELETE FROM "groups" WHERE id = ?', (int(group_id),))
    return str(group.get("name") or "group")


def add_pages_to_group(group_id: int, page_ids: Sequence[int]) -> int:
    ids = list(page_ids)
    if not ids:
        return 0
    now = utc_now()
    added = 0
    with db.transaction():
        for page_id in ids:
            cursor = db.execute(
                "INSERT INTO group_pages (group_id, page_id, added_at) VALUES (?, ?, ?)"
                " ON CONFLICT(group_id, page_id) DO NOTHING",
                (int(group_id), int(page_id), now),
            )
            added += int(cursor.rowcount or 0)
        db.execute('UPDATE "groups" SET updated_at = ? WHERE id = ?', (now, int(group_id)))
    return added


def remove_page_from_group(group_id: int, page_id: int) -> None:
    now = utc_now()
    with db.transaction():
        db.execute(
            "DELETE FROM group_pages WHERE group_id = ? AND page_id = ?",
            (int(group_id), int(page_id)),
        )
        db.execute('UPDATE "groups" SET updated_at = ? WHERE id = ?', (now, int(group_id)))


def set_page_tracked(page_ids: Sequence[int], tracked: bool) -> int:
    ids = list(page_ids)
    if not ids:
        return 0
    now = utc_now()
    with db.transaction():
        db.execute(
            f"UPDATE pages SET is_tracked = ?, updated_at = ?"
            f" WHERE id IN ({_placeholders(ids)})",
            [1 if tracked else 0, now, *ids],
        )
    return len(ids)


# ---------------------------------------------------------------------------
# product derivation — ads grouped URL-wise into products
# ---------------------------------------------------------------------------
def derive_products_for_page(conn, page_id: int, now: str | None = None) -> dict:
    """Group a page's ads into products by *normalized* destination URL.

    The raw URL stays verbatim on ``ads.destination_url`` (source fidelity);
    only the grouping key is normalized — tracking params (``fbclid``,
    ``utm_*``, ``gclid``, ...) and fragments are stripped so one product page
    never becomes many products.

    Idempotent: re-running only links new ads and bumps ``last_seen_at``.
    Products the owner removed permanently (``product_removed``) are never
    resurrected. ``conn`` is the caller's sqlite3 connection — this function
    never opens or commits its own transaction.
    """
    stamp = now or utc_now()
    page_id = int(page_id or 0)
    if page_id <= 0:
        return {"products": 0, "adsLinked": 0, "urls": 0}

    rows = conn.execute(
        "SELECT id, destination_url FROM ads"
        " WHERE page_id = ? AND COALESCE(destination_url, '') <> ''",
        (page_id,),
    ).fetchall()

    by_url: dict[str, list[int]] = {}
    for row in rows:
        ad_id = int(row[0])
        normalized = normalize_product_url(row[1])
        if normalized:
            by_url.setdefault(normalized, []).append(ad_id)

    removed_hashes = {
        str(r[0])
        for r in conn.execute("SELECT identity_hash FROM product_removed").fetchall()
    }

    products_touched = 0
    ads_linked = 0
    for url in sorted(by_url):
        identity_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()
        if identity_hash in removed_hashes:
            continue
        try:
            host = url.split("://", 1)[1].split("/", 1)[0].split("?", 1)[0].lower()
        except IndexError:
            host = ""
        display_name = product_display_name_from_url(url) or host or url[:80]

        conn.execute(
            "INSERT INTO products(normalized_name, display_name, product_url, domain,"
            " first_seen_at, last_seen_at, created_at, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(normalized_name) DO UPDATE SET"
            " last_seen_at = excluded.last_seen_at,"
            " updated_at = excluded.updated_at",
            (url, display_name, url, host, stamp, stamp, stamp, stamp),
        )
        # Look the id up instead of trusting lastrowid: an ON CONFLICT DO UPDATE
        # does not reliably report the conflicting row's id.
        found = conn.execute(
            "SELECT id FROM products WHERE normalized_name = ?", (url,)
        ).fetchone()
        if found is None:  # pragma: no cover - defensive
            continue
        product_id = int(found[0])
        # Fill blanks left by older rows without clobbering anything set.
        conn.execute(
            "UPDATE products SET display_name = CASE WHEN COALESCE(display_name,'') = ''"
            " THEN ? ELSE display_name END,"
            " product_url = CASE WHEN COALESCE(product_url,'') = ''"
            " THEN ? ELSE product_url END,"
            " domain = CASE WHEN COALESCE(domain,'') = '' THEN ? ELSE domain END"
            " WHERE id = ?",
            (display_name, url, host, product_id),
        )
        products_touched += 1
        for ad_id in by_url[url]:
            link = conn.execute(
                "INSERT OR IGNORE INTO ad_products(ad_id, product_id, method,"
                " confidence, created_at) VALUES(?, ?, 'url', 1.0, ?)",
                (ad_id, product_id, stamp),
            )
            ads_linked += int(link.rowcount or 0)

    return {"products": products_touched, "adsLinked": ads_linked, "urls": len(by_url)}


__all__ = [name for name in dir() if not name.startswith("_")]
