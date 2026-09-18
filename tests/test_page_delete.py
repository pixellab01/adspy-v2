"""Tests for page_service.delete_page() — permanent per-page deletion.

Covers the approved design:
* impact preview counts (what the confirmation modal shows)
* full cascade: ads, versions, product links, orphan products, scan history,
  metrics, snapshots, keyword links, pending targets, group memberships
* shared products and pipeline (shortlisted) products are kept
* finished queue targets keep their history, unlinked via SET NULL
* deletion is refused while a scan is running
"""

import pytest

from app import db, page_service

NOW = "2026-09-18T12:00:00Z"


def _seed():
    """Two pages sharing one product; page A carries the full footprint."""
    db.execute(
        "INSERT INTO pages (platform_page_id, name, created_at, updated_at)"
        " VALUES ('111', 'Page A', ?, ?)",
        (NOW, NOW),
    )
    db.execute(
        "INSERT INTO pages (platform_page_id, name, created_at, updated_at)"
        " VALUES ('222', 'Page B', ?, ?)",
        (NOW, NOW),
    )
    a = db.fetch_one("SELECT id FROM pages WHERE platform_page_id='111'")["id"]
    b = db.fetch_one("SELECT id FROM pages WHERE platform_page_id='222'")["id"]

    # ads: 3 on A (ad1 gets 2 versions), 1 on B
    ad_ids = {}
    for key, pid, lib in [("a1", a, "L1"), ("a2", a, "L2"), ("a3", a, "L3"), ("b1", b, "L4")]:
        cur = db.execute(
            "INSERT INTO ads (page_id, library_id, first_captured_at,"
            " last_captured_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (pid, lib, NOW, NOW, NOW, NOW),
        )
        ad_ids[key] = cur.lastrowid
    for ver, h in [(1, "h1"), (2, "h2")]:
        db.execute(
            "INSERT INTO ad_versions (ad_id, version_number, content_hash, captured_at)"
            " VALUES (?, ?, ?, ?)",
            (ad_ids["a1"], ver, h, NOW),
        )
    db.execute(
        "INSERT INTO ad_versions (ad_id, version_number, content_hash, captured_at)"
        " VALUES (?, 1, 'h3', ?)",
        (ad_ids["a2"], NOW),
    )

    # products: P1 orphan on A (deletable), P2 shared A+B (kept),
    # P3 orphan on A but shortlisted (kept by pipeline rule)
    prods = {}
    for key, norm, state in [
        ("p1", "prod-one", None),
        ("p2", "prod-two", None),
        ("p3", "prod-three", "shortlisted"),
    ]:
        cur = db.execute(
            "INSERT INTO products (normalized_name, display_name, shortlist_state,"
            " first_seen_at, last_seen_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (norm, norm, state, NOW, NOW, NOW, NOW),
        )
        prods[key] = cur.lastrowid
    db.execute(
        "INSERT INTO product_states (product_id, updated_at) VALUES (?, ?)",
        (prods["p1"], NOW),
    )
    links = [("a1", "p1"), ("a2", "p1"), ("a3", "p2"), ("b1", "p2"), ("a1", "p3")]
    for ad_key, prod_key in links:
        db.execute(
            "INSERT INTO ad_products (ad_id, product_id, created_at) VALUES (?, ?, ?)",
            (ad_ids[ad_key], prods[prod_key], NOW),
        )

    # page-level rows for A
    db.execute(
        "INSERT INTO page_scan_history (page_id, captured_at) VALUES (?, ?)", (a, NOW)
    )
    db.execute(
        "INSERT INTO page_scan_history (page_id, captured_at) VALUES (?, ?)",
        (a, "2026-09-17T12:00:00Z"),
    )
    db.execute(
        "INSERT INTO page_daily_metrics (page_id, metric_date) VALUES (?, ?)",
        (a, "2026-09-18"),
    )
    db.execute(
        "INSERT INTO page_daily_metrics (page_id, metric_date) VALUES (?, ?)",
        (a, "2026-09-17"),
    )
    db.execute(
        "INSERT INTO page_identity (page_id, logical_key, updated_at) VALUES (?, 'k1', ?)",
        (a, NOW),
    )
    db.execute("INSERT INTO page_states (page_id, updated_at) VALUES (?, ?)", (a, NOW))
    db.execute(
        "INSERT INTO alert_page_baselines (page_id, recorded_at) VALUES (?, ?)",
        (a, NOW),
    )
    db.execute(
        "INSERT INTO product_scan_snapshots (product_id, page_id, scanned_at, created_at)"
        " VALUES (?, ?, ?, ?)",
        (prods["p1"], a, NOW, NOW),
    )

    # keyword footprint for A
    q = db.execute(
        "INSERT INTO keyword_queries (keyword, created_at, updated_at)"
        " VALUES ('astrotalk.store', ?, ?)",
        (NOW, NOW),
    ).lastrowid
    run = db.execute(
        "INSERT INTO keyword_runs (query_id, requested_at) VALUES (?, ?)", (q, NOW)
    ).lastrowid
    db.execute(
        "INSERT INTO keyword_discovered_pages (query_id, page_id, platform_page_id,"
        " created_at, updated_at) VALUES (?, ?, '111', ?, ?)",
        (q, a, NOW, NOW),
    )
    db.execute(
        "INSERT INTO keyword_results (run_id, page_id) VALUES (?, ?)", (run, a)
    )

    # jobs: one pending target + one done target for A
    job = db.execute(
        "INSERT INTO jobs (job_type, idempotency_key, created_at, updated_at)"
        " VALUES ('page_scan', 'k-page-del-1', ?, ?)",
        (NOW, NOW),
    ).lastrowid
    db.execute(
        "INSERT INTO job_targets (job_id, position, page_id, status)"
        " VALUES (?, 1, ?, 'pending')",
        (job, a),
    )
    done_target = db.execute(
        "INSERT INTO job_targets (job_id, position, page_id, status)"
        " VALUES (?, 2, ?, 'done')",
        (job, a),
    ).lastrowid

    # brand group membership + a session link for A
    grp = db.execute(
        "INSERT INTO \"groups\" (name, created_at, updated_at) VALUES ('G1', ?, ?)",
        (NOW, NOW),
    ).lastrowid
    db.execute(
        "INSERT INTO group_pages (group_id, page_id, added_at) VALUES (?, ?, ?)",
        (grp, a, NOW),
    )
    sess = db.execute(
        "INSERT INTO sessions (session_number, name, started_at, created_at)"
        " VALUES (1, 'S1', ?, ?)",
        (NOW, NOW),
    ).lastrowid
    db.execute(
        "INSERT INTO session_pages (session_id, page_id, first_seen_at, last_seen_at)"
        " VALUES (?, ?, ?, ?)",
        (sess, a, NOW, NOW),
    )

    return {"a": a, "b": b, "job": job, "done_target": done_target, "prods": prods}


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("ADSPY2_DB_PATH", str(tmp_path / "t.sqlite3"))
    db.get_db()  # opens + migrates
    ids = _seed()
    yield ids
    db.close_db()


def _count(tbl, where="", params=()):
    row = db.fetch_one(f"SELECT COUNT(*) c FROM {tbl} {where}", params)
    return int(row["c"])


def test_impact_counts(seeded):
    imp = page_service.delete_page_impact(seeded["a"])
    assert imp["ads"] == 3
    assert imp["ad_versions"] == 3
    assert imp["products_linked"] == 3  # p1, p2, p3
    assert imp["products_orphaned"] == 1  # only p1 (p3 is shortlisted)
    assert imp["products_shared"] == 2
    assert imp["scan_history"] == 2
    assert imp["daily_metrics"] == 2
    assert imp["snapshots"] == 1
    assert imp["kw_discovered"] == 1
    assert imp["kw_results"] == 1
    assert imp["pending_targets"] == 1
    assert imp["group_memberships"] == 1

    many = page_service.delete_page_impact_many([seeded["a"], seeded["b"]])
    assert many[seeded["b"]]["ads"] == 1
    assert many[seeded["b"]]["products_orphaned"] == 0  # p2 shared with A


def test_delete_page_cascade(seeded):
    a, b = seeded["a"], seeded["b"]
    p1, p2, p3 = (seeded["prods"][k] for k in ("p1", "p2", "p3"))

    summary = page_service.delete_page(a)
    assert summary["page_id"] == a
    assert summary["ads_deleted"] == 3
    assert summary["products_deleted"] == 1
    assert summary["targets_cancelled"] == 1

    # page + its ad tree are gone
    assert _count("pages", "WHERE id=?", (a,)) == 0
    assert _count("ads", "WHERE page_id=?", (a,)) == 0
    assert _count("ad_versions") == 0
    assert _count("ad_products") == 1  # only b1->p2 survives
    # page-level satellites are gone
    for tbl in (
        "page_identity",
        "page_states",
        "page_scan_history",
        "page_daily_metrics",
        "product_scan_snapshots",
        "keyword_discovered_pages",
        "keyword_results",
        "group_pages",
        "session_pages",
        "alert_page_baselines",
    ):
        assert _count(tbl, "WHERE page_id=?", (a,)) == 0, tbl

    # orphan product p1 gone with its state row; shared p2 + shortlisted p3 kept
    assert _count("products", "WHERE id=?", (p1,)) == 0
    assert _count("product_states", "WHERE product_id=?", (p1,)) == 0
    assert _count("products", "WHERE id=?", (p2,)) == 1
    assert _count("products", "WHERE id=?", (p3,)) == 1

    # pending target deleted; done target kept as history, unlinked
    assert _count("job_targets", "WHERE page_id=?", (a,)) == 0
    done = db.fetch_one(
        "SELECT page_id, status FROM job_targets WHERE id=?", (seeded["done_target"],)
    )
    assert done["status"] == "done" and done["page_id"] is None

    # page B completely untouched
    assert _count("pages", "WHERE id=?", (b,)) == 1
    assert _count("ads", "WHERE page_id=?", (b,)) == 1

    # deleting twice raises not-found
    with pytest.raises(page_service.PageNotFoundError):
        page_service.delete_page(a)


def test_delete_page_blocked_while_running(seeded):
    a = seeded["a"]
    db.execute("UPDATE pages SET current_scan_status='running' WHERE id=?", (a,))
    with pytest.raises(page_service.PageError) as exc:
        page_service.delete_page(a)
    assert exc.value.code == "PAGE_SCAN_RUNNING"
    assert _count("pages", "WHERE id=?", (a,)) == 1  # untouched

    db.execute("UPDATE pages SET current_scan_status='idle' WHERE id=?", (a,))
    db.execute(
        "UPDATE job_targets SET status='running' WHERE page_id=? AND status='pending'",
        (a,),
    )
    with pytest.raises(page_service.PageError) as exc2:
        page_service.delete_page(a)
    assert exc2.value.code == "PAGE_SCAN_RUNNING"


def test_delete_page_not_found(seeded):
    with pytest.raises(page_service.PageNotFoundError):
        page_service.delete_page(999999)
