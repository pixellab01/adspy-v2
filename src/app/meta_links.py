"""Meta Ad Library URL build/parse helpers.

Ported from meta_main14/services/meta_links.py, plus the page-identity helpers
that ingest and the "add page" form both need.

The one rule that matters (docs/04 R5, docs/03 §3.1): a page's identity is
Facebook's own numeric page id, taken from ``view_all_page_id``. Never an
internal row id, never a URL, never a slug. A page-scan batch whose pageId is
not numeric is rejected outright — a wrong id merges ads onto the wrong page
and that is unrecoverable.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse, unquote

# v1 accepted 3+ digits for display/link purposes; ingest requires 5+ for a
# page-scan batch (config.NUMERIC_PAGE_ID_MIN_DIGITS).
_NUMERIC_PAGE_ID = re.compile(r"^\d{3,}$")
_STRICT_PAGE_ID = re.compile(r"^\d{5,}$")

# Query params that have carried a page id in Ad Library URLs, best first.
_PAGE_ID_PARAMS = ("view_all_page_id", "page_id", "id")

NAME_IDENTITY_PREFIX = "name:"


def is_numeric_page_id(value: Any) -> bool:
    """True for a plausible Meta page id (3+ digits) — display/link grade."""
    return bool(_NUMERIC_PAGE_ID.fullmatch(str(value or "").strip()))


def is_strict_page_id(value: Any) -> bool:
    """True for an ingest-grade Meta page id (``\\d{5,}``). Page-scan batches
    that fail this MUST be rejected."""
    return bool(_STRICT_PAGE_ID.fullmatch(str(value or "").strip()))


def page_id_from_url(page_url: Any) -> str:
    """Pull the numeric page id out of an Ad Library URL, '' when absent."""
    raw_url = str(page_url or "").strip()
    if not raw_url:
        return ""
    try:
        parsed = urlparse(raw_url if "://" in raw_url else "https://" + raw_url)
        params = parse_qs(parsed.query)
    except (TypeError, ValueError):
        return ""
    for key in _PAGE_ID_PARAMS:
        candidate = str((params.get(key) or [""])[0]).strip()
        if _NUMERIC_PAGE_ID.fullmatch(candidate):
            return candidate
    return ""


def meta_page_id(platform_page_id: Any = None, page_url: Any = None) -> str:
    """Validated numeric Meta page id from stored identity fields, else ''."""
    direct = str(platform_page_id or "").strip()
    if _NUMERIC_PAGE_ID.fullmatch(direct):
        return direct
    return page_id_from_url(page_url)


def meta_ads_library_url(platform_page_id: Any = None, page_url: Any = None) -> str:
    """Canonical 'all active ads for this page' Ad Library URL, '' when we have
    no numeric id (we never fabricate a link)."""
    page_id = meta_page_id(platform_page_id, page_url)
    if not page_id:
        return ""
    query = urlencode(
        {
            "active_status": "active",
            "ad_type": "all",
            "country": "ALL",
            "view_all_page_id": page_id,
        }
    )
    return f"https://www.facebook.com/ads/library/?{query}"


def meta_ad_url(library_id: Any) -> str:
    """Deep link to a single ad ('Open in Ad Library' in the drawer)."""
    ad_id = str(library_id or "").strip()
    if not ad_id:
        return ""
    query = urlencode({"id": ad_id, "country": "ALL", "ad_type": "all"})
    return f"https://www.facebook.com/ads/library/?{query}"


def meta_keyword_search_url(keyword: Any, country: str = "ALL") -> str:
    """Ad Library keyword-search URL (job_type 'keyword', Phase 4)."""
    query_text = str(keyword or "").strip()
    if not query_text:
        return ""
    query = urlencode(
        {
            "active_status": "active",
            "ad_type": "all",
            "country": country or "ALL",
            "q": query_text,
            "search_type": "keyword_unordered",
        }
    )
    return f"https://www.facebook.com/ads/library/?{query}"


def normalize_name(value: Any) -> str:
    """Lowercased, accent-folded, whitespace-collapsed name — used for the
    ``pages.normalized_name`` column and for name-identity hashing."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    return re.sub(r"\s+", " ", text)


def name_identity(name: Any) -> str:
    """Fallback page identity when Meta gives us no numeric id:
    ``name:<sha1(normalized_name)[:20]>``. Stable, so re-scans merge instead of
    creating twins. '' for an empty name — callers must then error out rather
    than invent a page."""
    normalized = normalize_name(name)
    if not normalized:
        return ""
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:20]
    return f"{NAME_IDENTITY_PREFIX}{digest}"


def is_name_identity(value: Any) -> bool:
    return str(value or "").startswith(NAME_IDENTITY_PREFIX)


def page_link_payload(
    *,
    page_id: Any,
    platform_page_id: Any = None,
    page_url: Any = None,
) -> dict[str, Any]:
    """Everything a template needs to render a page's two links."""
    numeric_id = meta_page_id(platform_page_id, page_url)
    external = meta_ads_library_url(numeric_id)
    try:
        internal_id = int(page_id or 0)
    except (TypeError, ValueError):
        internal_id = 0
    return {
        "internal_page_url": f"/pages/{internal_id}" if internal_id > 0 else "",
        "external_meta_url": external,
        "platform_page_id": numeric_id or str(platform_page_id or "").strip(),
        "can_open_external": bool(external),
    }


# ---------------------------------------------------------------------------
# product URL normalization — one product page, one product row
# ---------------------------------------------------------------------------
# Ad Library CTA links usually arrive with tracking junk appended
# (?fbclid=..., &utm_source=..., #fragments). Grouping ads on the verbatim URL
# would split a single product page into many "products". The raw URL stays
# verbatim on ads.destination_url (source fidelity); only the GROUPING key —
# products.product_url / normalized_name — is normalized here.
_TRACKING_QUERY_PARAMS = frozenset({
    # Meta / Google / Microsoft / TikTok click ids
    "fbclid", "gclid", "gclsrc", "dclid", "wbraid", "gbraid",
    "msclkid", "ttclid", "igshid", "twclid", "li_fat_id",
    # Mailchimp / generic
    "mc_cid", "mc_eid", "_openstat",
    # Meta ad-template leftovers sometimes seen on destination URLs
    "fb_action_ids", "fb_action_types", "fb_source", "fb_ref",
})

_UTM_PREFIX = "utm_"


def _is_tracking_param(name: str) -> bool:
    lname = str(name or "").strip().lower()
    return lname in _TRACKING_QUERY_PARAMS or lname.startswith(_UTM_PREFIX)


def normalize_product_url(url: Any) -> str:
    """Canonical grouping key for a product landing page.

    - lowercases scheme + host, drops default ports,
    - drops the #fragment,
    - drops tracking query params (fbclid, gclid, utm_*, ...),
    - keeps the remaining query params, sorted, for stability,
    - strips a trailing slash (except the root path).

    Returns '' when the URL is empty or has no usable host.
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        candidate = raw if "://" in raw else "https://" + raw
        parsed = urlparse(candidate)
    except (TypeError, ValueError):
        return ""
    host = (parsed.hostname or "").strip().lower()
    if not host or not re.fullmatch(r"[a-z0-9.-]+", host) or "." not in host:
        return ""
    scheme = (parsed.scheme or "https").lower()
    if scheme not in ("http", "https"):
        return ""
    # Drop default ports; keep the rest.
    port = parsed.port
    netloc = host
    if port and not (scheme == "http" and port == 80) and not (scheme == "https" and port == 443):
        netloc = f"{host}:{port}"
    path = parsed.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    try:
        kept: list[tuple[str, str]] = []
        for key, values in parse_qs(parsed.query, keep_blank_values=False).items():
            if _is_tracking_param(key):
                continue
            for value in values:
                kept.append((key, value))
        kept.sort(key=lambda kv: (kv[0].lower(), kv[1]))
        query = urlencode(kept, doseq=False)
    except (TypeError, ValueError):
        query = ""
    return urlunparse((scheme, netloc, path, "", query, ""))


def product_display_name_from_url(normalized_url: Any) -> str:
    """Human-readable product name derived from the (normalized) product URL.

    Uses the last path segment — ``/products/mala-108-beads`` -> ``Mala 108 Beads``.
    Falls back to the host when the path carries no name.
    """
    raw = str(normalized_url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw if "://" in raw else "https://" + raw)
    except (TypeError, ValueError):
        return ""
    segments = [seg for seg in (parsed.path or "").split("/") if seg]
    name = ""
    if segments:
        name = unquote(segments[-1])
        # strip common page extensions: product.html, item.php, ...
        name = re.sub(r"\.(html?|php|aspx?|jsp)$", "", name, flags=re.IGNORECASE)
    if not name:
        name = parsed.hostname or ""
    name = re.sub(r"[-_+]+", " ", name).strip()
    name = re.sub(r"\s+", " ", name)
    if not name:
        return ""
    # Title-case gently: keep all-caps tokens (SKUs) as they are.
    words = []
    for word in name.split(" "):
        words.append(word if word.isupper() and len(word) > 1 else word.capitalize())
    return " ".join(words)[:160]
