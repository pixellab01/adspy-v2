"""Page lifecycle service — permanent page deletion.

``delete_page()`` removes a page and everything collected for it:

* the page row itself (``pages``)
* its ads — ad versions, product links, transcript links and language rows
  cascade via FK (``PRAGMA foreign_keys = ON``)
* products that become orphaned (no ad of any other page links to them) and
  are not in the decision pipeline (``shortlist_state IS NULL``) — their
  states, meta, metrics, shortlist and queue rows cascade via FK
* scan history, daily metrics, product scan snapshots
* keyword discovery links and keyword results attributed to the page
* brand-group memberships, session links, alert baselines
* pending queue targets for the page (they never ran — cancelled)

Kept on purpose:

* products shared with other pages (only unlinked from this page's ads),
  including any product already in the decision pipeline
* finished/failed/skipped queue targets — job history stays, the FK
  (``ON DELETE SET NULL``) unlinks them from the gone page
* keyword search/run history itself

Refuses while a scan is running for the page — deleting under a live worker
would corrupt the scan. Mirrors the ``delete_job()`` guard in
``job_service.py``.
"""

from __future__ import annotations

from typing import Any

from .db import execute, fetch_all, fetch_one, transaction


class PageError(Exception):
    """Domain error with a machine-readable ``code`` for the UI."""

    def __init__(self, message: str, code: str = "PAGE_ERROR"):
        super().__init__(message)
        self.code = code


class PageNotFoundError(PageError):
    def __init__(self, page_id: int):
        super().__init__(f"page {page_id} does not exist", code="PAGE_NOT_FOUND")


# ---------------------------------------------------------------------------
# impact preview — what the delete confirmation modal shows
# ---------------------------------------------------------------------------

_IMPACT_ZERO = {
    "ads": 0,
    "ad_versions": 0,
    "products_linked": 0,
    "products_orphaned": 0,
    "products_shared": 0,
    "scan_history": 0,
    "daily_metrics": 0,
    "snapshots": 0,
    "kw_discovered": 0,
    "kw_results": 0,
    "pending_targets": 0,
    "group_memberships": 0,
}


def _grouped(sql: str, page_ids: list[int]) -> dict[int, int]:
    """Run a ``SELECT page_id, COUNT(*) ... GROUP BY page_id`` query."""
    if not page_ids:
        return {}
    placeholders = ",".join("?" for _ in page_ids)
    out: dict[int, int] = {}
    for row in fetch_all(sql.format(placeholders=placeholders), page_ids):
        out[int(row[0])] = int(row[1])
    return out


def delete_page_impact_many(page_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Per-page delete-impact counts for the Page Analyzer list.

    One small indexed query per metric — no per-row round trips. Pages with
    no rows in a metric simply report 0 for it.
    """
    ids = [int(p) for p in page_ids]
    impacts: dict[int, dict[str, Any]] = {
        pid: dict(_IMPACT_ZERO) for pid in ids
    }
    if not ids:
        return impacts

    ads = _grouped(
        "SELECT page_id, COUNT(*) FROM ads "
        "WHERE page_id IN ({placeholders}) GROUP BY page_id",
        ids,
    )
    versions = _grouped(
        "SELECT a.page_id, COUNT(*) FROM ad_versions v "
        "JOIN ads a ON a.id = v.ad_id "
        "WHERE a.page_id IN ({placeholders}) GROUP BY a.page_id",
        ids,
    )
    linked = _grouped(
        "SELECT a.page_id, COUNT(DISTINCT ap.product_id) FROM ad_products ap "
        "JOIN ads a ON a.id = ap.ad_id "
        "WHERE a.page_id IN ({placeholders}) GROUP BY a.page_id",
        ids,
    )
    orphaned = _grouped(
        "SELECT a.page_id, COUNT(DISTINCT ap.product_id) FROM ad_products ap "
        "JOIN ads a ON a.id = ap.ad_id "
        "JOIN products p ON p.id = ap.product_id "
        "WHERE a.page_id IN ({placeholders}) "
        "AND p.shortlist_state IS NULL "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM ad_products ap2 "
        "  JOIN ads a2 ON a2.id = ap2.ad_id "
        "  WHERE ap2.product_id = ap.product_id AND a2.page_id != a.page_id"
        ") GROUP BY a.page_id",
        ids,
    )
    history = _grouped(
        "SELECT page_id, COUNT(*) FROM page_scan_history "
        "WHERE page_id IN ({placeholders}) GROUP BY page_id",
        ids,
    )
    metrics = _grouped(
        "SELECT page_id, COUNT(*) FROM page_daily_metrics "
        "WHERE page_id IN ({placeholders}) GROUP BY page_id",
        ids,
    )
    snapshots = _grouped(
        "SELECT page_id, COUNT(*) FROM product_scan_snapshots "
        "WHERE page_id IN ({placeholders}) GROUP BY page_id",
        ids,
    )
    kw_disc = _grouped(
        "SELECT page_id, COUNT(*) FROM keyword_discovered_pages "
        "WHERE page_id IN ({placeholders}) GROUP BY page_id",
        ids,
    )
    kw_res = _grouped(
        "SELECT page_id, COUNT(*) FROM keyword_results "
        "WHERE page_id IN ({placeholders}) GROUP BY page_id",
        ids,
    )
    pending = _grouped(
        "SELECT page_id, COUNT(*) FROM job_targets "
        "WHERE page_id IN ({placeholders}) AND status = 'pending' "
        "GROUP BY page_id",
        ids,
    )
    groups = _grouped(
        "SELECT page_id, COUNT(*) FROM group_pages "
        "WHERE page_id IN ({placeholders}) GROUP BY page_id",
        ids,
    )

    for pid in ids:
        imp = impacts[pid]
        imp["ads"] = ads.get(pid, 0)
        imp["ad_versions"] = versions.get(pid, 0)
        imp["products_linked"] = linked.get(pid, 0)
        imp["products_orphaned"] = orphaned.get(pid, 0)
        imp["products_shared"] = imp["products_linked"] - imp["products_orphaned"]
        imp["scan_history"] = history.get(pid, 0)
        imp["daily_metrics"] = metrics.get(pid, 0)
        imp["snapshots"] = snapshots.get(pid, 0)
        imp["kw_discovered"] = kw_disc.get(pid, 0)
        imp["kw_results"] = kw_res.get(pid, 0)
        imp["pending_targets"] = pending.get(pid, 0)
        imp["group_memberships"] = groups.get(pid, 0)
    return impacts


def delete_page_impact(page_id: int) -> dict[str, Any]:
    """Impact counts for a single page (used by the delete summary)."""
    return delete_page_impact_many([int(page_id)]).get(int(page_id), dict(_IMPACT_ZERO))


# ---------------------------------------------------------------------------
# the delete itself
# ---------------------------------------------------------------------------

_PENDING_TARGET_STATUSES = ("pending",)


def delete_page(page_id: int) -> dict[str, Any]:
    """Delete a page and all data collected for it.

    Refuses when a scan is currently running for the page. Returns a summary
    ``{page_id, page_name, ads_deleted, ad_versions_deleted, products_deleted,
    products_kept_shared, targets_cancelled, ...}``.
    """
    page_id = int(page_id)
    with transaction():
        page = fetch_one(
            "SELECT id, COALESCE(NULLIF(alias, ''), name) AS display_name, "
            "platform_page_id, current_scan_status "
            "FROM pages WHERE id = ?",
            (page_id,),
        )
        if page is None:
            raise PageNotFoundError(page_id)
        page_name = page["display_name"] or f"Page {page['platform_page_id']}"

        if str(page["current_scan_status"]) == "running":
            raise PageError(
                f'"{page_name}" ka scan abhi chal raha hai — '
                "scan complete hone ke baad delete karna",
                code="PAGE_SCAN_RUNNING",
            )
        live_target = fetch_one(
            "SELECT id FROM job_targets WHERE page_id = ? AND status = 'running' "
            "LIMIT 1",
            (page_id,),
        )
        if live_target is not None:
            raise PageError(
                f'"{page_name}" ka scan abhi chal raha hai — '
                "scan complete hone ke baad delete karna",
                code="PAGE_SCAN_RUNNING",
            )

        impact = delete_page_impact(page_id)

        # Pending targets never ran — cancel them outright so no NULL-page
        # target is left behind for the worker to choke on. Finished/failed/
        # skipped targets keep their job history; the FK unlinks them.
        placeholders = ",".join("?" for _ in _PENDING_TARGET_STATUSES)
        targets_cancelled = int(
            execute(
                f"DELETE FROM job_targets WHERE page_id = ? "
                f"AND status IN ({placeholders})",
                (page_id, *_PENDING_TARGET_STATUSES),
            ).rowcount
            or 0
        )

        # This page's ads; versions / product links / transcript links /
        # language rows cascade via FK.
        ad_ids = [
            int(r[0])
            for r in fetch_all("SELECT id FROM ads WHERE page_id = ?", (page_id,))
        ]
        candidate_products: list[int] = []
        if ad_ids:
            placeholders = ",".join("?" for _ in ad_ids)
            candidate_products = [
                int(r[0])
                for r in fetch_all(
                    f"SELECT DISTINCT product_id FROM ad_products "
                    f"WHERE ad_id IN ({placeholders})",
                    ad_ids,
                )
            ]
        ads_deleted = int(
            execute("DELETE FROM ads WHERE page_id = ?", (page_id,)).rowcount or 0
        )

        # Orphan products go; shared ones (and anything already in the
        # decision pipeline) stay. Product states/meta/metrics/shortlist
        # cascade via FK.
        products_deleted = 0
        if candidate_products:
            placeholders = ",".join("?" for _ in candidate_products)
            products_deleted = int(
                execute(
                    f"DELETE FROM products "
                    f"WHERE shortlist_state IS NULL "
                    f"AND id IN ({placeholders}) "
                    f"AND id NOT IN (SELECT DISTINCT product_id FROM ad_products)",
                    candidate_products,
                ).rowcount
                or 0
            )

        # The page row itself. Everything else page-linked cascades via FK
        # (identity, states, scan history, daily metrics, snapshots, keyword
        # discovery/results, group memberships, session links, alert
        # baselines); finished queue targets, test-queue sources and block
        # records are unlinked via ON DELETE SET NULL by design.
        execute("DELETE FROM pages WHERE id = ?", (page_id,))

        return {
            "page_id": page_id,
            "page_name": page_name,
            "platform_page_id": page["platform_page_id"],
            "ads_deleted": ads_deleted,
            "ad_versions_deleted": impact["ad_versions"],
            "products_deleted": products_deleted,
            "products_kept_shared": impact["products_shared"],
            "targets_cancelled": targets_cancelled,
            "scan_history_deleted": impact["scan_history"],
            "snapshots_deleted": impact["snapshots"],
        }
