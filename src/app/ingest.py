"""Ingest + reconciliation — the crown jewel.

Ported from meta_main14/services/ingest_service.py (the ~370-line
``ingest_payload`` god-function plus its helpers). The *logic* is v1's, proven
on 17,579 real ads; only the shape changed: one entry point, five named steps,
and none of v1's workspace / session / profile-scope / cache-invalidation /
five-receipt-tables machinery.

Entry point:

    ingest_batch(job_id, batch) -> dict

Everything happens inside a single ``BEGIN IMMEDIATE`` transaction.

The rules that must never bend (docs/03 §3, docs/04 R3/R4/R5/R11):

  * A page-scan batch whose ``pageId`` is not a numeric Meta page id
    (``\\d{5,}``) is REJECTED outright. A wrong id merges ads onto the wrong
    page and that is unrecoverable.
  * Pages are never invented. Identity = numeric id > id parsed out of the URL >
    ``name:<sha1[:20]>`` fallback. No identity, no page.
  * Reconciliation (marking ads inactive) runs ONLY when the batch is final AND
    the job is a page scan AND the outcome is one of complete/exhausted/empty.
    Partial / blocked / failed batches, keyword jobs, and finals with no stated
    outcome deactivate NOTHING. A false "complete" would kill hundreds of live
    ads.
  * The accepted set is the union of this batch and every previously accepted
    batch of THIS job for THIS page — a scan streams over many batches and only
    the last one is final.
  * A 600 second grace window protects ads captured by an overlapping wave.
  * Page totals are always COUNTed from the ads table. Client counts are never
    trusted for anything except the header estimate.

Deliberate deviations from v1, all documented at the bottom of the phase report:
  * an ad with no ``libraryId`` is skipped with a warning instead of getting a
    synthetic content-derived key (v1 minted one; it produced near-duplicates);
  * rejected batches are not persisted (v1 didn't either) — they raise;
  * ``ad_observations`` is gone (docs/03 §2): the signal lives in
    ``ads.last_captured_at`` + ``page_daily_metrics``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Any, Iterable, Iterator, Sequence

from . import config as app_config
from .db import get_db, transaction
from .meta_links import is_strict_page_id, name_identity, normalize_name, page_id_from_url
from .time_utils import utc_now

log = logging.getLogger(__name__)

__all__ = [
    "IngestRejected",
    "ingest_batch",
    "upsert_page",
    "upsert_ad",
    "reconcile_page",
    "record_page_daily_snapshot",
    "record_product_scan_snapshots",
    "content_hash",
    "collation_ceiling",
    "parse_start_date",
    "resolve_page_identity",
]


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
class IngestRejected(ValueError):
    """The batch was not acknowledged. Nothing was written.

    Carries a typed ``code`` so ``app/jobs.py`` can answer the extension with
    ``{"ok": false, "error": ..., "code": ...}`` instead of a bare 500.
    """

    def __init__(self, message: str, code: str = "batch_rejected") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# tiny helpers
# ---------------------------------------------------------------------------
def _pick(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """First key present with a non-empty value (v1's ``_pick``)."""
    for key in keys:
        if isinstance(data, dict) and data.get(key) not in (None, ""):
            return data[key]
    return default


def _text(value: Any, limit: int = 0) -> str:
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    return text[:limit] if limit else text


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _chunked(values: Sequence[Any], size: int = 400) -> Iterator[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _day(timestamp: str) -> str:
    """'2026-08-03' out of '2026-08-03T09:15:42+00:00'."""
    return str(timestamp or "")[:10]


# Page names Meta hands out when it has nothing real — never an identity.
_UNKNOWN_PAGE_NAMES = {
    "",
    "unknown",
    "unknown page",
    "facebook page",
    "meta page",
    "advertiser",
    "untitled",
    "n a",
    "na",
}


def _name_key(value: Any) -> str:
    """Loose comparison key for the placeholder guard (v1's ``_norm``)."""
    lowered = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", lowered).strip()


def _usable_page_name(value: Any, keyword: str = "") -> bool:
    """v1's fake-advertiser guard: a keyword is not a page name."""
    name = _text(value)
    if _name_key(name) in _UNKNOWN_PAGE_NAMES:
        return False
    if keyword and name.casefold() == _text(keyword).casefold():
        return False
    return bool(name)


# ---------------------------------------------------------------------------
# ad field normalisation
# ---------------------------------------------------------------------------
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# Hindi month prefixes (R6: the Ad Library renders localised labels).
_HINDI_MONTHS: tuple[tuple[str, int], ...] = (
    ("जन", 1),
    ("फ़र", 2),
    ("फर", 2),
    ("मार", 3),
    ("अप्रै", 4),
    ("मई", 5),
    ("जून", 6),
    ("जुल", 7),
    ("अग", 8),
    ("सित", 9),
    ("अक्तू", 10),
    ("अक्टू", 10),
    ("नव", 11),
    ("दिस", 12),
)

_START_LABEL_PREFIX = re.compile(
    r"^.*?(?:started running on|started running|start date|running since|started on"
    r"|शुरू होने की तारीख|पर चलना शुरू हुआ|से चल रहा है)\s*[:\-]?\s*",
    re.IGNORECASE,
)

_DATE_FORMATS = (
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%Y-%m-%d",
)


def parse_start_date(ad: dict[str, Any]) -> str | None:
    """Meta's real "Started running on" date — never the capture timestamp (R11).

    Returns ``YYYY-MM-DD`` or None. An unparseable label yields None so the
    existing value is preserved rather than replaced by today.
    """
    explicit = _pick(
        ad,
        "start_date",
        "startDate",
        "startedRunningDate",
        "started_running_date",
        "started_running_on",
        "startedAt",
    )
    if explicit not in (None, ""):
        text = str(explicit).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return text
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            pass

    raw = _text(
        _pick(
            ad,
            "startDateText",
            "start_date_text",
            "startedRunningRaw",
            "started_running_raw",
            "adStartDate",
            "startDateLabel",
            default="",
        )
    ).translate(_DEVANAGARI_DIGITS)
    if not raw:
        return None

    # Cards carry decoration around the date ("Library ID 123 · 12 Jul 2025",
    # "Started running on 3 Mar 2025 - ended 9 Apr 2025"). Try the whole label
    # first, then each separator-delimited piece, left to right, so the START
    # date wins over any end date sitting behind a dash.
    cleaned = _START_LABEL_PREFIX.sub("", raw).strip()
    candidates: list[str] = []
    for text in (cleaned, raw):
        for piece in [text, *re.split(r"[-|·•,;]", text)]:
            piece = piece.strip().strip(",")
            if piece and piece not in candidates:
                candidates.append(piece)

    for candidate in candidates:
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date().isoformat()
            except ValueError:
                continue

    # Localised "<day> <month-word> <year>" (Hindi and friends — R6).
    for candidate in candidates:
        match = re.search(r"(\d{1,2})\s*([^\d\s,]+)[\s,]*(\d{4})", candidate)
        if not match:
            continue
        day, token, year = match.group(1), match.group(2), match.group(3)
        for prefix, month in _HINDI_MONTHS:
            if token.startswith(prefix):
                try:
                    return datetime(int(year), month, int(day)).date().isoformat()
                except ValueError:
                    return None
    return None


def _media_urls(ad: dict[str, Any]) -> list[str]:
    raw = _pick(ad, "mediaUrls", "media_urls", "media", "mediaReferences", default=[])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = [raw]
    if isinstance(raw, dict):
        raw = [raw]
    urls: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            item = _pick(item, "url", "src", "videoUrl", "imageUrl", default="")
        url = str(item or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def _normalise_ad_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"inactive", "ended", "stopped", "off", "false", "0", "not_active"}:
        return "inactive"
    return "active"


def _ad_fields(ad: dict[str, Any]) -> dict[str, Any]:
    """The v2 ``ads`` columns extracted from one extension ad record."""
    return {
        "library_id": _text(
            _pick(
                ad,
                "libraryId",
                "library_id",
                "adLibraryId",
                "ad_library_id",
                "adArchiveID",
                "ad_archive_id",
                "adId",
                "id",
                default="",
            ),
            240,
        ),
        "status": _normalise_ad_status(_pick(ad, "status", default="active")),
        "start_date": parse_start_date(ad),
        "end_date": _pick(ad, "endDate", "end_date"),
        "ad_text": _pick(ad, "adText", "ad_text", "primaryText", "primary_text", "body"),
        "headline": _pick(ad, "headline", "title"),
        "description": _pick(ad, "description"),
        "cta": _pick(ad, "cta", "callToAction", "call_to_action"),
        "destination_url": _pick(ad, "destinationUrl", "destination_url", "link"),
        "media_type": (_text(_pick(ad, "mediaType", "media_type", default="")) or None),
        "media_urls": _media_urls(ad),
        "represented_ad_count": max(
            1,
            _int(
                _pick(
                    ad,
                    "representedAdCount",
                    "represented_ad_count",
                    "effectiveAdCount",
                    "effective_ad_count",
                    default=1,
                ),
                1,
            ),
        ),
    }


def content_hash(fields: dict[str, Any]) -> str:
    """Creative fingerprint. A change here means a new ``ad_versions`` row."""
    parts = [
        str(fields.get("ad_text") or ""),
        str(fields.get("headline") or ""),
        str(fields.get("description") or ""),
        str(fields.get("cta") or ""),
        str(fields.get("destination_url") or ""),
        json.dumps(fields.get("media_urls") or [], sort_keys=True, ensure_ascii=False),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# STEP: page identity + upsert
# ---------------------------------------------------------------------------
def resolve_page_identity(page: dict[str, Any], keyword: str = "") -> dict[str, Any]:
    """Turn a loose page candidate into ``{platform_page_id, name, url, ...}``.

    Order (docs/03 §3.3): explicit numeric id > id parsed out of the page URL >
    ``name:<sha1[:20]>``. Raises ``IngestRejected`` when nothing usable is left —
    a page is NEVER invented.
    """
    nested: dict[str, Any] = {}
    for key in ("page", "advertiser", "advertiserPage", "advertiser_page"):
        value = page.get(key) if isinstance(page, dict) else None
        if isinstance(value, dict):
            nested = value
            break

    raw_id = _text(
        _pick(
            page,
            "pageId",
            "page_id",
            "platformPageId",
            "platform_page_id",
            default=_pick(nested, "pageId", "page_id", "platformPageId", "id", default=""),
        )
    )
    url = _text(
        _pick(
            page,
            "pageUrl",
            "page_url",
            "url",
            default=_pick(nested, "pageUrl", "page_url", "url", default=""),
        )
    )
    name = _text(
        _pick(
            page,
            "pageName",
            "page_name",
            "name",
            "advertiserName",
            "advertiser_name",
            default=_pick(nested, "pageName", "page_name", "name", default=""),
        ),
        300,
    )

    # Some extension builds put the whole FB URL in pageId (v1 saw this).
    if raw_id.startswith(("http://", "https://")):
        url = url or raw_id
        raw_id = page_id_from_url(raw_id)
    if not raw_id and url:
        raw_id = page_id_from_url(url)

    platform_page_id = raw_id
    identity_source = "page_id"
    if not platform_page_id:
        if not _usable_page_name(name, keyword):
            raise IngestRejected(
                "Page identity is missing a numeric Meta page id, a page URL, and a usable name.",
                code="page_identity_missing",
            )
        platform_page_id = name_identity(name)
        identity_source = "name_hash"
        if not platform_page_id:
            raise IngestRejected(
                "Page identity could not be derived from the supplied name.",
                code="page_identity_missing",
            )

    return {
        "platform_page_id": platform_page_id,
        "name": name,
        "url": url or None,
        "profile_image_url": _pick(
            page,
            "profileImageUrl",
            "profile_image_url",
            default=_pick(nested, "profileImageUrl", "profile_image_url"),
        ),
        "identity_source": identity_source,
    }


def _is_keyword_placeholder(identity: dict[str, Any], keyword: str) -> bool:
    """v1's fake-advertiser bug guard: a keyword-named page with no real id."""
    if identity.get("identity_source") != "name_hash":
        return False
    return not _usable_page_name(identity.get("name"), keyword)


def upsert_page(
    conn, page: dict[str, Any], now: str | None = None, keyword: str = "", *, tracked: bool = True
) -> int:
    """Insert or update a page on its natural key ``platform_page_id``.

    ``tracked=False`` (keyword stage 1) inserts a NEW page as ``is_tracked=0``:
    a page a keyword surfaced is a *candidate* in the review list, not a
    tracked page, until the owner accepts it (stage 2). An existing page keeps
    whatever ``is_tracked`` it has — discovery never untracks.

    Returns the internal ``pages.id``. Counts are NOT written here — they are
    always derived later by :func:`_publish_page_totals`.
    """
    now = now or utc_now()
    identity = page if "platform_page_id" in page else resolve_page_identity(page, keyword)
    platform_page_id = identity["platform_page_id"]
    name = _text(identity.get("name"), 300)
    url = identity.get("url") or None

    row = conn.execute(
        "SELECT id, name FROM pages WHERE platform_page_id=?",
        (platform_page_id,),
    ).fetchone()

    if row:
        page_id = int(row[0])
        # Never blank out a good name with an empty/placeholder one.
        if not _usable_page_name(name, keyword):
            name = str(row[1] or "")
        conn.execute(
            """
            UPDATE pages
            SET name=?,
                normalized_name=?,
                url=COALESCE(NULLIF(?, ''), url),
                profile_image_url=COALESCE(?, profile_image_url),
                last_captured_at=?,
                first_captured_at=COALESCE(first_captured_at, ?),
                updated_at=?
            WHERE id=?
            """,
            (
                name,
                normalize_name(name),
                url or "",
                identity.get("profile_image_url"),
                now,
                now,
                now,
                page_id,
            ),
        )
        return page_id

    if not name:
        name = platform_page_id
    cursor = conn.execute(
        """
        INSERT INTO pages(
            platform_page_id, name, normalized_name, url, profile_image_url,
            is_tracked, is_hidden, current_scan_status,
            last_captured_at, first_captured_at, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,0,'idle',?,?,?,?)
        """,
        (
            platform_page_id,
            name,
            normalize_name(name),
            url,
            identity.get("profile_image_url"),
            1 if tracked else 0,
            now,
            now,
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# STEP: ad upsert (+ versions, + revival)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# B5: the ceiling on represented_ad_count
# ---------------------------------------------------------------------------
#
# ``represented_ad_count`` is a number the extension reads off the card's
# "N ads use this creative" line, and until 2026-08-16 NOTHING bounded it
# anywhere in the pipeline — extractor, batcher and ingest were all
# ``max(1, ...)`` with no upper limit. Every one of those values is summed into
# ``pages.represented_ads`` by :func:`_page_totals`, which is the "Ads scraped"
# column the owner reads, and it feeds the coverage grade that decides whether
# a scan may retire ads. A single misparse therefore moves a page-level number
# and can cost live ads.
#
# It did misparse. The old regex ``(\d[\d,.\s]*)\s+ads?\s+use...`` ran over the
# card's FLATTENED text, so a digit run on the preceding line was swallowed:
# "Library ID: 333333333333333 / 7 ads use this creative" parsed as
# 3,333,333,333,333,337. The extractor fix (inject/extractor.js B1/B2) stops
# that at the source; this is the last place it can be stopped before SUM().
#
# The bound: no single creative can stand for more ads than Facebook says the
# whole page has. ``fb_estimated_results`` is that number; ``MIN_COLLATION_
# CEILING`` covers pages we have never seen a header for. A value above the
# ceiling is not clamped to it — a card claiming to be the entire page is not
# "a bit high", it is unreadable — so it is refused and stored as 1, and logged.
MIN_COLLATION_CEILING = 50


def collation_ceiling(fb_estimated_results: Any) -> int:
    """Largest believable ``represented_ad_count`` for one card on this page."""
    return max(MIN_COLLATION_CEILING, _int(fb_estimated_results))


def _bounded_represented(conn, page_id: int, library_id: str, value: int) -> int:
    """``value``, or 1 when it exceeds what the page could possibly hold."""
    count = max(1, _int(value, 1))
    if count <= MIN_COLLATION_CEILING:
        return count
    row = conn.execute(
        "SELECT fb_estimated_results FROM pages WHERE id=?", (int(page_id),)
    ).fetchone()
    ceiling = collation_ceiling(row[0] if row else 0)
    if count <= ceiling:
        return count
    log.warning(
        "ad %s on page %s reported represented_ad_count=%s, above the page ceiling "
        "of %s — refused, stored as 1",
        library_id, page_id, count, ceiling,
    )
    return 1


def upsert_ad(conn, ad: dict[str, Any], page_id: int, now: str | None = None) -> tuple[int, bool, bool]:
    """Insert or update one ad on its natural key ``library_id``.

    Returns ``(ad_id, created, content_changed)``.

    * ``content_hash`` changed  -> a new ``ad_versions`` row (next version_number)
    * previously inactive ad reappearing -> REVIVAL: status back to ``active``
      and ``end_date`` cleared.
    * ``start_date`` is only ever filled in, never overwritten with NULL.
    """
    now = now or utc_now()
    fields = _ad_fields(ad)
    library_id = fields["library_id"]
    if not library_id:
        raise IngestRejected("Ad has no libraryId.", code="ad_identity_missing")

    fields["represented_ad_count"] = _bounded_represented(
        conn, page_id, library_id, fields["represented_ad_count"]
    )

    digest = _text(_pick(ad, "contentHash", "content_hash", default="")) or content_hash(fields)
    media_json = json.dumps(fields["media_urls"], ensure_ascii=False)
    end_date = None if fields["status"] == "active" else (fields["end_date"] or _day(now))

    row = conn.execute(
        "SELECT id, content_hash FROM ads WHERE library_id=?",
        (library_id,),
    ).fetchone()

    if row:
        ad_id = int(row[0])
        content_changed = str(row[1] or "") != digest
        if content_changed:
            _append_ad_version(conn, ad_id, digest, fields, media_json, now, "Content hash changed")
        conn.execute(
            """
            UPDATE ads
            SET page_id=?,
                status=?,
                start_date=COALESCE(?, start_date),
                end_date=?,
                ad_text=?,
                headline=?,
                description=?,
                cta=?,
                destination_url=?,
                media_type=COALESCE(?, media_type),
                media_urls=?,
                represented_ad_count=?,
                content_hash=?,
                last_captured_at=?,
                updated_at=?
            WHERE id=?
            """,
            (
                int(page_id),
                fields["status"],
                fields["start_date"],
                end_date,
                fields["ad_text"],
                fields["headline"],
                fields["description"],
                fields["cta"],
                fields["destination_url"],
                fields["media_type"],
                media_json,
                fields["represented_ad_count"],
                digest,
                now,
                now,
                ad_id,
            ),
        )
        return ad_id, False, content_changed

    cursor = conn.execute(
        """
        INSERT INTO ads(
            page_id, library_id, status, start_date, end_date, ad_text, headline,
            description, cta, destination_url, media_type, media_urls,
            represented_ad_count, content_hash, first_captured_at, last_captured_at,
            created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(page_id),
            library_id,
            fields["status"],
            fields["start_date"],
            end_date,
            fields["ad_text"],
            fields["headline"],
            fields["description"],
            fields["cta"],
            fields["destination_url"],
            fields["media_type"],
            media_json,
            fields["represented_ad_count"],
            digest,
            now,
            now,
            now,
            now,
        ),
    )
    ad_id = int(cursor.lastrowid)
    _append_ad_version(conn, ad_id, digest, fields, media_json, now, "Initial capture")
    return ad_id, True, True


def _append_ad_version(
    conn,
    ad_id: int,
    digest: str,
    fields: dict[str, Any],
    media_json: str,
    now: str,
    change_summary: str,
) -> None:
    """Next-numbered creative snapshot. ``INSERT OR IGNORE`` because
    ``UNIQUE(ad_id, content_hash)`` means a revert to an older creative reuses
    the version row that already records it."""
    version_number = int(
        conn.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 FROM ad_versions WHERE ad_id=?",
            (ad_id,),
        ).fetchone()[0]
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO ad_versions(
            ad_id, version_number, content_hash, captured_at, ad_text, headline,
            description, cta, destination_url, media_urls, change_summary
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ad_id,
            version_number,
            digest,
            now,
            fields["ad_text"],
            fields["headline"],
            fields["description"],
            fields["cta"],
            fields["destination_url"],
            media_json,
            change_summary,
        ),
    )


# ---------------------------------------------------------------------------
# STEP: reconciliation
# ---------------------------------------------------------------------------
def _job_union_ad_ids(conn, job_id: int, page_id: int, exclude_batch_id: str) -> set[int]:
    """Ads accepted by earlier batches of THIS job for THIS page.

    A page snapshot streams across many batches and only the last one is final
    (R4). Everything the earlier batches acknowledged belongs to the same live
    snapshot, so the final pass must never stop it. v1 called this the
    session-union; with v2's job-scoped protocol it is a job-union, read
    straight off ``job_batches.accepted_ad_ids_json``.
    """
    rows = conn.execute(
        """
        SELECT accepted_ad_ids_json
        FROM job_batches
        WHERE job_id=? AND page_id=? AND status='accepted' AND batch_id<>?
        """,
        (int(job_id), int(page_id), str(exclude_batch_id or "")),
    ).fetchall()

    library_ids: set[str] = set()
    for row in rows:
        try:
            values = json.loads(row[0] or "[]")
        except (TypeError, ValueError):
            continue
        if isinstance(values, list):
            for value in values:
                text = str(value or "").strip()
                if text:
                    library_ids.add(text)
    if not library_ids:
        return set()

    ad_ids: set[int] = set()
    ordered = sorted(library_ids)
    for chunk in _chunked(ordered):
        placeholders = ",".join("?" for _ in chunk)
        ad_ids.update(
            int(row[0])
            for row in conn.execute(
                f"SELECT id FROM ads WHERE page_id=? AND library_id IN ({placeholders})",
                (int(page_id), *chunk),
            ).fetchall()
        )
    return ad_ids


def reconcile_page(
    conn,
    *,
    page_id: int,
    accepted_ad_ids: Iterable[int],
    now: str,
    grace_seconds: int | None = None,
    return_ids: bool = False,
) -> int | list[int]:
    """Stop every previously-active ad Meta no longer shows for this page.

    Only ever called for a final page-scan batch with a complete/exhausted/empty
    outcome. Ads captured inside the grace window survive even when they are not
    in the accepted set — they belong to an overlapping capture wave. Returns
    how many ads were stopped. A stopped ad that reappears later is revived by
    :func:`upsert_ad`.

    With ``return_ids=True`` the stopped ad ids are returned instead of the
    count — the product snapshot recorder needs them to attribute stops to
    products.
    """
    grace = app_config.RECONCILE_GRACE_SECONDS if grace_seconds is None else int(grace_seconds)
    keep = {int(value) for value in accepted_ad_ids if int(value) > 0}

    candidates = [
        int(row[0])
        for row in conn.execute(
            """
            SELECT id FROM ads
            WHERE page_id=?
              AND status='active'
              AND datetime(last_captured_at) < datetime(?, ?)
            """,
            (int(page_id), now, f"-{grace} seconds"),
        ).fetchall()
    ]
    victims = [ad_id for ad_id in candidates if ad_id not in keep]
    if not victims:
        return victims if return_ids else 0

    end_date = _day(now)
    for chunk in _chunked(victims):
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(
            f"""
            UPDATE ads
            SET status='inactive', end_date=?, updated_at=?
            WHERE id IN ({placeholders})
            """,
            (end_date, now, *chunk),
        )
    return victims if return_ids else len(victims)


# ---------------------------------------------------------------------------
# STEP: derived page totals + daily snapshot
# ---------------------------------------------------------------------------
def _page_totals(conn, page_id: int) -> tuple[int, int, int]:
    """(total_ads, active_ads, represented_ads) COUNTed from the ads table.

    The client's own numbers are never trusted here — this is what makes the
    dashboard true even mid-scan.
    """
    row = conn.execute(
        """
        SELECT COUNT(*),
               COALESCE(SUM(CASE WHEN status='active' THEN 1 ELSE 0 END), 0),
               COALESCE(SUM(CASE WHEN status='active' THEN represented_ad_count ELSE 0 END), 0)
        FROM ads WHERE page_id=?
        """,
        (int(page_id),),
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)


def _publish_page_totals(
    conn,
    page_id: int,
    now: str,
    *,
    final: bool,
    estimated_results: int,
    new_ads_this_job: int,
    stopped_ads: int,
) -> tuple[int, int, int]:
    total, active, represented = _page_totals(conn, page_id)
    conn.execute(
        """
        UPDATE pages
        SET total_ads=?,
            active_ads=?,
            represented_ads=?,
            fb_estimated_results=CASE WHEN ?>0 THEN ? ELSE fb_estimated_results END,
            current_scan_status=?,
            last_captured_at=?,
            updated_at=?
        WHERE id=?
        """,
        (
            total,
            active,
            represented,
            int(estimated_results),
            int(estimated_results),
            "idle" if final else "running",
            now,
            now,
            int(page_id),
        ),
    )
    if final:
        # The "change since last scan" delta column (decision #6) — the whole
        # of v2's daily news, in four columns.
        conn.execute(
            """
            UPDATE pages
            SET last_delta=? - prev_active_ads,
                last_new_ads=?,
                last_stopped_ads=?,
                last_verified_at=?
            WHERE id=?
            """,
            (active, int(new_ads_this_job), int(stopped_ads), now, int(page_id)),
        )
    return total, active, represented


def record_page_daily_snapshot(conn, page_id: int, now: str | None = None) -> None:
    """Upsert today's ``page_daily_metrics`` row (7D sparkline + history).

    Idempotent: re-running on the same UTC day refreshes the row from the ads
    table rather than adding to it.
    """
    now = now or utc_now()
    metric_date = _day(now)
    row = conn.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN status='active' THEN 1 ELSE 0 END), 0),
               COALESCE(SUM(CASE WHEN status='active' THEN represented_ad_count ELSE 0 END), 0),
               COALESCE(SUM(CASE WHEN date(first_captured_at)=? THEN 1 ELSE 0 END), 0),
               COALESCE(SUM(CASE WHEN COALESCE(end_date,'')<>'' AND date(end_date)=? THEN 1 ELSE 0 END), 0)
        FROM ads WHERE page_id=?
        """,
        (metric_date, metric_date, int(page_id)),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO page_daily_metrics(
            page_id, metric_date, active_ads, new_ads, stopped_ads, represented_ads
        ) VALUES(?,?,?,?,?,?)
        ON CONFLICT(page_id, metric_date) DO UPDATE SET
            active_ads=excluded.active_ads,
            new_ads=excluded.new_ads,
            stopped_ads=excluded.stopped_ads,
            represented_ads=excluded.represented_ads
        """,
        (
            int(page_id),
            metric_date,
            int(row[0] or 0),
            int(row[2] or 0),
            int(row[3] or 0),
            int(row[1] or 0),
        ),
    )


def record_product_scan_snapshots(
    conn,
    *,
    page_id: int,
    job_id: int,
    now: str,
    stopped_ad_ids: Iterable[int],
) -> int:
    """Write one ``product_scan_snapshots`` row per product on this page.

    Called once per reconciled page-scan target, right after the page totals
    and the daily snapshot. Each row freezes this scan's view of one product
    on one page: how many of its ads are live, how many are new, how many
    just stopped. Two consecutive rows are all the history UI needs for the
    growth %.

    ``stopped_ad_ids`` are the ids :func:`reconcile_page` just marked
    inactive (``return_ids=True``) — attributing stops to products from the
    ids is exact, unlike re-deriving them from ``end_date``. A product whose
    every ad stopped still gets a row (active 0) so the graph shows the drop
    instead of a gap.
    """
    stopped = {int(v) for v in stopped_ad_ids if int(v) > 0}

    scan_start = now
    target_row = conn.execute(
        """
        SELECT started_at FROM job_targets
        WHERE job_id=? AND page_id=?
        ORDER BY id DESC LIMIT 1
        """,
        (int(job_id), int(page_id)),
    ).fetchone()
    if target_row and target_row[0]:
        scan_start = target_row[0]
    else:
        job_row = conn.execute(
            "SELECT created_at FROM jobs WHERE id=?", (int(job_id),)
        ).fetchone()
        if job_row and job_row[0]:
            scan_start = job_row[0]

    product_ids = {
        int(r[0])
        for r in conn.execute(
            """
            SELECT DISTINCT ap.product_id
            FROM ad_products ap
            JOIN ads a ON a.id = ap.ad_id
            WHERE a.page_id=?
            """,
            (int(page_id),),
        ).fetchall()
    }
    if stopped:
        placeholders = ",".join("?" for _ in stopped)
        for r in conn.execute(
            f"SELECT DISTINCT product_id FROM ad_products WHERE ad_id IN ({placeholders})",
            tuple(stopped),
        ).fetchall():
            product_ids.add(int(r[0]))
    if not product_ids:
        return 0

    stopped_marks = ",".join("?" for _ in stopped) if stopped else "NULL"
    rows = conn.execute(
        f"""
        SELECT ap.product_id AS product_id,
               COUNT(DISTINCT CASE WHEN a.status='active' THEN a.id END) AS active_ads,
               COUNT(DISTINCT CASE WHEN datetime(a.first_captured_at) >= datetime(?)
                                   THEN a.id END) AS new_ads,
               COUNT(DISTINCT CASE WHEN a.id IN ({stopped_marks}) THEN a.id END) AS stopped_ads
        FROM ad_products ap
        JOIN ads a ON a.id = ap.ad_id
        WHERE a.page_id=? AND ap.product_id IN ({",".join("?" for _ in product_ids)})
        GROUP BY ap.product_id
        """,
        (scan_start, *(() if not stopped else tuple(stopped)), int(page_id), *sorted(product_ids)),
    ).fetchall()

    written = 0
    for r in rows:
        conn.execute(
            """
            INSERT INTO product_scan_snapshots(
                product_id, page_id, job_id, scanned_at,
                active_ads, new_ads, stopped_ads, created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(product_id, page_id, job_id) DO UPDATE SET
                scanned_at=excluded.scanned_at,
                active_ads=excluded.active_ads,
                new_ads=excluded.new_ads,
                stopped_ads=excluded.stopped_ads,
                created_at=excluded.created_at
            """,
            (
                int(r[0]), int(page_id), int(job_id), now,
                int(r[1] or 0), int(r[2] or 0), int(r[3] or 0), now,
            ),
        )
        written += 1
    return written


# ---------------------------------------------------------------------------
# STEP: batch normalisation (the hard-reject lives here)
# ---------------------------------------------------------------------------
def _normalise_batch(job_id: int, batch: dict[str, Any], job_type: str) -> dict[str, Any]:
    """Validate the wire payload (docs/04 §4) and fail loudly on junk.

    This is where the two rules that protect the database from unrecoverable
    damage are enforced:

      R5  page-scan pageId MUST be a numeric Meta page id (``\\d{5,}``);
      R4  reconciliation needs BOTH ``isFinal`` and a final outcome.
    """
    if not isinstance(batch, dict):
        raise IngestRejected("Batch payload must be an object.", code="bad_payload")

    batch_id = _text(_pick(batch, "batchId", "batch_id", default=""), 220)
    if not batch_id:
        raise IngestRejected("Batch is missing batchId.", code="batch_id_missing")

    outcome = _text(_pick(batch, "outcome", default="")).lower() or None
    if outcome and outcome not in app_config.VALID_OUTCOMES:
        raise IngestRejected(f"Unknown outcome {outcome!r}.", code="bad_outcome")

    ads = [item for item in (batch.get("ads") or []) if isinstance(item, dict)]
    keyword = _text(_pick(batch, "keyword", "query", default=""), 200)
    if job_type == "keyword" and not keyword:
        # The placeholder guard needs the search term; an older extension
        # build never sent it. The keyword job's single target is labelled
        # with the term (keyword_service.start_run), so read it from there.
        row = get_db().execute(
            "SELECT label FROM job_targets WHERE job_id=? ORDER BY position LIMIT 1",
            (int(job_id),),
        ).fetchone()
        keyword = _text(row[0] if row else "", 200)

    page_identity: dict[str, Any] | None = None
    if job_type == "page_scan":
        raw_page_id = _text(_pick(batch, "pageId", "page_id", default=""))
        page_url = _text(_pick(batch, "pageUrl", "page_url", default=""))
        candidate = raw_page_id
        if candidate.startswith(("http://", "https://")):
            candidate = page_id_from_url(candidate)
        if not candidate and page_url:
            candidate = page_id_from_url(page_url)
        if not is_strict_page_id(candidate):
            raise IngestRejected(
                "A page-scan batch requires a numeric Meta Page ID "
                f"(\\d{{{app_config.NUMERIC_PAGE_ID_MIN_DIGITS},}}); got {raw_page_id!r}. "
                "The batch was not acknowledged.",
                code="page_id_not_numeric",
            )
        page_identity = resolve_page_identity(
            {
                "pageId": candidate,
                "pageName": _pick(batch, "pageName", "page_name"),
                "pageUrl": page_url,
                "profileImageUrl": _pick(batch, "profileImageUrl", "profile_image_url"),
            }
        )
        if _is_keyword_placeholder(page_identity, keyword):
            raise IngestRejected(
                "Refusing to create a keyword-placeholder page.",
                code="keyword_placeholder_page",
            )

    return {
        "job_id": int(job_id),
        "job_type": job_type,
        "batch_id": batch_id,
        "batch_sequence": _int(_pick(batch, "batchSequence", "batch_sequence", default=0)),
        "target_position": (
            None
            if _pick(batch, "targetPosition", "target_position") is None
            else _int(_pick(batch, "targetPosition", "target_position", default=0))
        ),
        "page_identity": page_identity,
        "keyword": keyword,
        "ads": ads,
        "represented_ad_count": _int(
            _pick(batch, "representedAdCount", "represented_ad_count", default=0)
        ),
        "estimated_results": _int(
            _pick(batch, "estimatedResults", "estimated_results", default=0)
        ),
        # A keyword search is never a complete snapshot of any page, so a
        # keyword batch is never final: nothing to reconcile, and Sessions /
        # Logs must not show one as such. The extension already forces this at
        # the source; the server does not trust it to.
        "is_final": (
            False
            if job_type == "keyword"
            else _bool(_pick(batch, "isFinal", "is_final", "finalBatch", "snapshotComplete"))
        ),
        "is_final_ignored": (
            job_type == "keyword"
            and _bool(_pick(batch, "isFinal", "is_final", "finalBatch", "snapshotComplete"))
        ),
        "outcome": outcome,
    }


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def ingest_batch(job_id: int, batch: dict) -> dict:
    """Ingest one extension batch. The only public write path into ads/pages.

    Returns::

        {"status": "accepted"|"duplicate", "adsSeen": int, "adsNew": int,
         "adsUpdated": int, "adsDeactivated": int, "pageId": int|None,
         "warnings": [str], ...}

    plus ``batchId``, ``acceptedAdLibraryIds``, ``reconciled``, ``activeAds``,
    ``representedAds`` and ``totalAds`` for ``app/jobs.py``'s HTTP answer.

    Raises :class:`IngestRejected` (a ``ValueError``) and writes nothing when the
    batch is not trustworthy.
    """
    # THE FREEZE (app/dataset.py): while OLD DATA is the active dataset no scan
    # may write. Checked here, where the batch write lands, so no route-level
    # bypass exists — nothing below runs: no receipt, no ads, no ad_versions.
    from . import dataset as _dataset

    if not _dataset.scan_writes_allowed():
        raise IngestRejected(_dataset.frozen_message(), code="DATASET_FROZEN")

    conn = get_db()
    job = conn.execute("SELECT id, job_type, status FROM jobs WHERE id=?", (int(job_id),)).fetchone()
    if job is None:
        raise IngestRejected(f"Unknown job {job_id}.", code="job_not_found")
    job_type = str(job[1] or "page_scan")

    parsed = _normalise_batch(job_id, batch, job_type)
    now = utc_now()
    warnings: list[str] = []
    if str(job[2] or "") in {"completed", "cancelled", "failed"}:
        warnings.append(f"job is {job[2]}; batch accepted anyway")
    if parsed.get("is_final_ignored"):
        warnings.append(
            "isFinal ignored: a keyword batch is never final "
            f"(outcome={parsed['outcome']!r}) — nothing deactivated"
        )

    with transaction():
        conn = get_db()

        # --- 1. idempotency: one receipt per (job_id, batch_id) -------------
        replay = _load_receipt(conn, parsed["job_id"], parsed["batch_id"])
        if replay is not None:
            return replay

        # --- 2. page ---------------------------------------------------------
        page_id: int | None = None
        if parsed["page_identity"] is not None:
            page_id = upsert_page(conn, parsed["page_identity"], now, parsed["keyword"])

        first_batch_for_page = False
        if page_id is not None:
            first_batch_for_page = not conn.execute(
                "SELECT 1 FROM job_batches WHERE job_id=? AND page_id=? AND status='accepted' LIMIT 1",
                (parsed["job_id"], page_id),
            ).fetchone()
            if first_batch_for_page:
                # Freeze the "before" figure so the final batch can publish an
                # honest "change since last scan" delta.
                conn.execute(
                    "UPDATE pages SET prev_active_ads=active_ads WHERE id=?",
                    (page_id,),
                )

        # --- 3. ads ----------------------------------------------------------
        accepted_library_ids: list[str] = []
        batch_ad_ids: set[int] = set()
        ads_new = 0
        ads_updated = 0
        skipped_missing_page = 0
        skipped_missing_id = 0

        for raw_ad in parsed["ads"]:
            target_page_id = page_id
            if target_page_id is None:
                # Keyword jobs (R12): each ad carries its own page candidate.
                # An unresolvable ad is skipped — never a fabricated page.
                try:
                    identity = resolve_page_identity(raw_ad, parsed["keyword"])
                    if _is_keyword_placeholder(identity, parsed["keyword"]):
                        raise IngestRejected("keyword placeholder", code="keyword_placeholder_page")
                    target_page_id = upsert_page(
                        conn, identity, now, parsed["keyword"], tracked=False
                    )
                except IngestRejected:
                    skipped_missing_page += 1
                    continue

            try:
                ad_id, created, _changed = upsert_ad(conn, raw_ad, target_page_id, now)
            except IngestRejected:
                skipped_missing_id += 1
                continue

            batch_ad_ids.add(ad_id)
            ads_new += 1 if created else 0
            ads_updated += 0 if created else 1
            library_id = str(
                conn.execute("SELECT library_id FROM ads WHERE id=?", (ad_id,)).fetchone()[0]
            )
            if library_id and library_id not in accepted_library_ids:
                accepted_library_ids.append(library_id)

        if skipped_missing_page:
            warnings.append(f"{skipped_missing_page} ad(s) skipped: no resolvable page identity")
        if skipped_missing_id:
            warnings.append(f"{skipped_missing_id} ad(s) skipped: no libraryId")

        # --- 3b. product derivation (URL-wise grouping) ----------------------
        # Ads are grouped into products by NORMALIZED destination URL —
        # tracking params (fbclid, utm_*, gclid, ...) are stripped for the
        # grouping key only; ads.destination_url stays verbatim. A derive bug
        # must never fail the batch: the ads are already safely stored.
        try:
            from .product_service import derive_products_for_page as _derive

            derive_page_ids: set[int] = set()
            if page_id is not None:
                derive_page_ids.add(int(page_id))
            elif batch_ad_ids:
                placeholders = ",".join("?" for _ in batch_ad_ids)
                for prow in conn.execute(
                    f"SELECT DISTINCT page_id FROM ads WHERE id IN ({placeholders})",
                    tuple(batch_ad_ids),
                ).fetchall():
                    if prow[0]:
                        derive_page_ids.add(int(prow[0]))
            derived_products = 0
            derived_links = 0
            for derive_pid in sorted(derive_page_ids):
                stats = _derive(conn, derive_pid, now)
                derived_products += int(stats.get("products") or 0)
                derived_links += int(stats.get("adsLinked") or 0)
            if derived_products:
                warnings.append(
                    f"derived {derived_products} product(s),"
                    f" {derived_links} ad link(s) URL-wise"
                )
        except Exception as exc:  # noqa: BLE001 - derive is best-effort
            log.warning("product derivation failed for job %s: %r", job_id, exc)
            warnings.append("product derivation skipped (internal error; ads kept)")

        # --- 4. reconciliation (the only place ads ever go inactive) --------
        reconcile = (
            parsed["is_final"]
            and job_type == "page_scan"
            and parsed["outcome"] in app_config.FINAL_OUTCOMES
            and page_id is not None
        )
        if parsed["is_final"] and not reconcile:
            warnings.append(
                "final batch did not reconcile "
                f"(job_type={job_type}, outcome={parsed['outcome']!r}) — nothing deactivated"
            )

        ads_deactivated = 0
        stopped_ad_ids: list[int] = []
        if reconcile:
            accepted_ids = set(batch_ad_ids)
            accepted_ids |= _job_union_ad_ids(conn, parsed["job_id"], page_id, parsed["batch_id"])
            stopped_ad_ids = reconcile_page(
                conn,
                page_id=page_id,
                accepted_ad_ids=accepted_ids,
                now=now,
                return_ids=True,
            )
            ads_deactivated = len(stopped_ad_ids)

        # --- 5. derived totals, snapshot, receipt ---------------------------
        active_ads = represented_ads = total_ads = 0
        if page_id is not None:
            new_ads_this_job = ads_new + _int(
                conn.execute(
                    """
                    SELECT COALESCE(SUM(ads_new), 0) FROM job_batches
                    WHERE job_id=? AND page_id=? AND status='accepted' AND batch_id<>?
                    """,
                    (parsed["job_id"], page_id, parsed["batch_id"]),
                ).fetchone()[0]
            )
            total_ads, active_ads, represented_ads = _publish_page_totals(
                conn,
                page_id,
                now,
                final=reconcile,
                estimated_results=parsed["estimated_results"],
                new_ads_this_job=new_ads_this_job,
                stopped_ads=ads_deactivated,
            )
            record_page_daily_snapshot(conn, page_id, now)
            if reconcile:
                # Per-product scan history ("18 the, ab 21"): one snapshot row
                # per product on this page. Best-effort like product
                # derivation — a snapshot bug must never fail the batch.
                try:
                    record_product_scan_snapshots(
                        conn,
                        page_id=page_id,
                        job_id=parsed["job_id"],
                        now=now,
                        stopped_ad_ids=stopped_ad_ids,
                    )
                except Exception:  # noqa: BLE001 - snapshots are derived data
                    log.exception(
                        "product snapshots failed for page %s job %s",
                        page_id, parsed["job_id"],
                    )

        result: dict[str, Any] = {
            "status": "accepted",
            "adsSeen": len(parsed["ads"]),
            "adsNew": ads_new,
            "adsUpdated": ads_updated,
            "adsDeactivated": ads_deactivated,
            "pageId": page_id,
            "warnings": warnings,
            "batchId": parsed["batch_id"],
            "acceptedAdLibraryIds": accepted_library_ids,
            "isFinal": parsed["is_final"],
            "outcome": parsed["outcome"],
            "reconciled": reconcile,
            "activeAds": active_ads,
            "representedAds": represented_ads,
            "totalAds": total_ads,
        }
        _save_receipt(conn, parsed, page_id, result, accepted_library_ids, now, batch)

    return result


# ---------------------------------------------------------------------------
# receipts — one row in job_batches, replayed verbatim on a duplicate
# ---------------------------------------------------------------------------
def _load_receipt(conn, job_id: int, batch_id: str) -> dict[str, Any] | None:
    """The extension parks and replays batches when the dashboard is down (R9).
    A batch already acknowledged is swallowed for free: same answer, no second
    ingest, no double counting."""
    row = conn.execute(
        """
        SELECT receipt_json FROM job_batches
        WHERE job_id=? AND batch_id=? AND status='accepted'
        """,
        (int(job_id), str(batch_id)),
    ).fetchone()
    if row is None:
        return None
    try:
        receipt = json.loads(row[0] or "{}")
    except (TypeError, ValueError):
        receipt = {}
    if not isinstance(receipt, dict):
        receipt = {}
    return {**receipt, "status": "duplicate", "batchId": str(batch_id)}


def _save_receipt(
    conn,
    parsed: dict[str, Any],
    page_id: int | None,
    result: dict[str, Any],
    accepted_library_ids: list[str],
    now: str,
    raw_batch: dict[str, Any],
) -> None:
    payload_hash = hashlib.sha256(
        json.dumps(raw_batch, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    conn.execute(
        """
        INSERT INTO job_batches(
            job_id, batch_id, batch_sequence, target_position, page_id, is_final,
            outcome, status, ads_seen, ads_new, ads_updated, ads_deactivated,
            represented_ad_count, payload_hash, accepted_ad_ids_json, receipt_json,
            received_at
        ) VALUES(?,?,?,?,?,?,?,'accepted',?,?,?,?,?,?,?,?,?)
        ON CONFLICT(job_id, batch_id) DO UPDATE SET
            status='accepted',
            receipt_json=excluded.receipt_json,
            accepted_ad_ids_json=excluded.accepted_ad_ids_json,
            received_at=excluded.received_at
        """,
        (
            parsed["job_id"],
            parsed["batch_id"],
            parsed["batch_sequence"],
            parsed["target_position"],
            page_id,
            1 if parsed["is_final"] else 0,
            parsed["outcome"],
            result["adsSeen"],
            result["adsNew"],
            result["adsUpdated"],
            result["adsDeactivated"],
            parsed["represented_ad_count"],
            payload_hash,
            json.dumps(accepted_library_ids, ensure_ascii=False),
            json.dumps(result, ensure_ascii=False, default=str),
            now,
        ),
    )
