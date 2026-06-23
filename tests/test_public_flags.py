"""lb-public-flags — visitor image flags + crowdsource publish-all.

Two features, one test module:

1. Publish-all (the public crowdsource gallery): on the PUBLIC_READONLY instance,
   the three crowdsource subjects (fysik/matematik/teknik) show approved OR
   un-reviewed images; every other subject stays approved-only; rejected images
   are hidden everywhere. The private instance is unaffected (shows all).

2. The visitor flag write (POST /api/flag) — the single sanctioned public write —
   plus the LAN-only review surface (GET /api/flags, POST /api/flags/<id>/resolve).

The headline guarantees: a visitor can only flag an image the public gallery
actually shows; the reason is sanitised + capped; the write is rate-limited +
idempotent; and the public instance can never READ flag state.
"""
from __future__ import annotations

import importlib
import uuid

import pytest

import app.public_visibility as pv


def _init_schema(db_path):
    """Create the schema once via the real migrations (read-write), no reload."""
    from app.db import init_db
    init_db(db_path)


def _insert(db_path, *, subject, area=None, review_ok=None, deleted=False,
            image_type="diagram", title="Bild", kind="bild"):
    """Insert a library row via a plain sqlite3 connection.

    Deliberately NOT app.db.connect — seeding must work regardless of whether the
    db module is currently in read-only mode in this test process.
    """
    import sqlite3
    # A real taxonomy area per subject — the gallery validates subject/area, so a
    # fake area would return [] independent of review_ok and mask the test.
    _AREA = {"fysik": "rorelse", "matematik": "taluppfattning",
             "teknik": "tekniska_system", "kemi": "materia"}
    if area is None:
        area = _AREA.get(subject, "rorelse")
    rid = str(uuid.uuid4())
    fp = f"output/{subject}/{area}/{image_type}/{rid}.png"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO library (id, kind, subject, area, image_type, title, "
            "file_path, created_at) VALUES (?,?,?,?,?,?,?, datetime('now'))",
            (rid, kind, subject, area, image_type, title, fp),
        )
        if review_ok is not None:
            conn.execute("UPDATE library SET review_ok=? WHERE id=?", (review_ok, rid))
        if deleted:
            conn.execute("UPDATE library SET deleted_at=datetime('now') WHERE id=?", (rid,))
        conn.commit()
    finally:
        conn.close()
    return rid, fp


def _read_flags(db_path, sql, params=()):
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# Env-sensitive modules that read LARBILD_PUBLIC_READONLY at IMPORT time. They
# (and the app factory) must be reloaded after the env is set so the public/
# private wiring + visibility predicate reflect the test's mode. app.db is
# NOT in this list — reloading it would reset _DB_PATH/_READ_ONLY mid-test.
_ENV_MODULES = (
    "app.public_visibility",
    "app.routes.library",
    "app.routes.media",
    "app.routes.flags",
    "app",
)


def _reload_env_modules():
    for name in _ENV_MODULES:
        importlib.reload(importlib.import_module(name))


def _public_app(db_path, monkeypatch):
    """A PUBLIC_READONLY app over an already-schema'd db_path.

    Reloads the env-sensitive modules so their import-time PUBLIC_READONLY reads
    flip true, then builds the factory. The factory's _init_database calls
    configure_db_path(DB_PATH) → pins app.db._DB_PATH to THIS db + flips read-only
    on, so a stale path left by a prior test's reload can't bleed through.
    """
    monkeypatch.setenv("LARBILD_PUBLIC_READONLY", "1")
    monkeypatch.setenv("LARBILD_DB_PATH", str(db_path))
    monkeypatch.setenv("LARBILD_FLAG_IP_SALT", "testsalt")
    _reload_env_modules()
    import app as _app_pkg
    app = _app_pkg.create_app({"TESTING": True, "DB_PATH": str(db_path),
                               "RECONCILE_STORAGE": False})
    # Belt-and-suspenders: assert the path actually pinned to this test's db.
    import app.db as _db
    assert str(_db._DB_PATH) == str(db_path), (_db._DB_PATH, db_path)
    return app


def _private_app(db_path, monkeypatch):
    monkeypatch.delenv("LARBILD_PUBLIC_READONLY", raising=False)
    monkeypatch.setenv("LARBILD_DB_PATH", str(db_path))
    _reload_env_modules()
    import app as _app_pkg
    app = _app_pkg.create_app({"TESTING": True, "DB_PATH": str(db_path),
                               "RECONCILE_STORAGE": False})
    import app.db as _db
    # Private factory runs init_db → pins _DB_PATH (read-write) to this db.
    assert str(_db._DB_PATH) == str(db_path), (_db._DB_PATH, db_path)
    return app


@pytest.fixture(autouse=True)
def _restore_modules():
    """Reset all env-sensitive module state to clean (non-public) BEFORE and
    AFTER each test, so a public-mode test never leaks ``_READ_ONLY=True`` (which
    would make the next test's schema-init write through a read-only connection)
    nor a stale ``LARBILD_PUBLIC_READONLY``. app.db is reloaded FIRST so its
    read-only flag + cached path reset to defaults."""
    import os
    os.environ.pop("LARBILD_PUBLIC_READONLY", None)
    importlib.reload(importlib.import_module("app.db"))
    _reload_env_modules()
    yield
    os.environ.pop("LARBILD_PUBLIC_READONLY", None)
    importlib.reload(importlib.import_module("app.db"))
    _reload_env_modules()


# ── 1. publish-all visibility ───────────────────────────────────────────────


def test_crowdsource_subject_publishes_unreviewed(tmp_path, monkeypatch):
    db = tmp_path / "lib.db"
    _init_schema(db)
    appr, _ = _insert(db, subject="fysik", area="rorelse", review_ok=1)
    pend, _ = _insert(db, subject="fysik", area="rorelse", review_ok=None)
    rej, _ = _insert(db, subject="fysik", area="rorelse", review_ok=0)
    app = _public_app(db, monkeypatch)
    r = app.test_client().get("/api/library/items?subject=fysik&area=rorelse")
    ids = {it["id"] for it in r.get_json()["items"]}
    assert appr in ids, "approved must show"
    assert pend in ids, "un-reviewed crowdsource image must show (publish-all)"
    assert rej not in ids, "rejected must stay hidden"


def test_non_crowdsource_subject_stays_approved_only(tmp_path, monkeypatch):
    db = tmp_path / "lib.db"
    _init_schema(db)
    appr, _ = _insert(db, subject="kemi", review_ok=1)
    pend, _ = _insert(db, subject="kemi", review_ok=None)
    app = _public_app(db, monkeypatch)
    r = app.test_client().get("/api/library/items?subject=kemi&area=materia")
    ids = {it["id"] for it in r.get_json()["items"]}
    assert appr in ids
    assert pend not in ids, "non-crowdsource un-reviewed must NOT publish"


def test_private_instance_shows_everything(tmp_path, monkeypatch):
    db = tmp_path / "lib.db"
    _init_schema(db)
    pend, _ = _insert(db, subject="kemi", review_ok=None)
    rej, _ = _insert(db, subject="kemi", review_ok=0)
    app = _private_app(db, monkeypatch)
    r = app.test_client().get("/api/library/items?subject=kemi&area=materia")
    ids = {it["id"] for it in r.get_json()["items"]}
    assert pend in ids and rej in ids, "private tool is unfiltered by review_ok"


# ── 2. the public flag write ────────────────────────────────────────────────


def test_flag_visible_image_ok(tmp_path, monkeypatch):
    db = tmp_path / "lib.db"
    _init_schema(db)
    vid, _ = _insert(db, subject="matematik", review_ok=None)
    app = _public_app(db, monkeypatch)
    r = app.test_client().post("/api/flag", json={"image_id": vid, "reason": "fel"})
    assert r.status_code == 201 and r.get_json() == {"ok": True}
    rows = _read_flags(db, "SELECT COUNT(*) AS n FROM flags WHERE library_id=?", (vid,))
    assert rows[0]["n"] == 1


def test_flag_rejected_or_unknown_is_404(tmp_path, monkeypatch):
    db = tmp_path / "lib.db"
    _init_schema(db)
    rej, _ = _insert(db, subject="matematik", review_ok=0)
    kemi, _ = _insert(db, subject="kemi", review_ok=None)
    app = _public_app(db, monkeypatch)
    cl = app.test_client()
    assert cl.post("/api/flag", json={"image_id": rej}).status_code == 404
    assert cl.post("/api/flag", json={"image_id": kemi}).status_code == 404
    assert cl.post("/api/flag", json={"image_id": "nope"}).status_code == 404
    assert cl.post("/api/flag", json={}).status_code == 400


def test_flag_reason_sanitised_and_capped(tmp_path, monkeypatch):
    db = tmp_path / "lib.db"
    _init_schema(db)
    vid, _ = _insert(db, subject="teknik", review_ok=None)
    app = _public_app(db, monkeypatch)
    bad = "<script>alert(1)</script>\x00\n\n " + "A" * 2000
    app.test_client().post("/api/flag", json={"image_id": vid, "reason": bad})
    reason = _read_flags(db, "SELECT reason FROM flags WHERE library_id=?", (vid,))[0]["reason"]
    assert "<script>" not in reason and "&lt;script&gt;" in reason
    assert "\x00" not in reason and "\n" not in reason
    assert len(reason) <= 600  # capped (escape can only modestly expand the 500 cap)


def test_flag_idempotent_per_source(tmp_path, monkeypatch):
    db = tmp_path / "lib.db"
    _init_schema(db)
    vid, _ = _insert(db, subject="fysik", review_ok=None)
    app = _public_app(db, monkeypatch)
    cl = app.test_client()
    for _ in range(5):
        cl.post("/api/flag", json={"image_id": vid}, headers={"X-Forwarded-For": "10.0.0.5"})
    n = _read_flags(db, "SELECT COUNT(*) AS n FROM flags WHERE library_id=? AND status='open'", (vid,))[0]["n"]
    assert n == 1, "duplicate open flags from one source collapse"


def test_flag_rate_limited(tmp_path, monkeypatch):
    db = tmp_path / "lib.db"
    _init_schema(db)
    ids = [_insert(db, subject="matematik", review_ok=None)[0] for _ in range(30)]
    app = _public_app(db, monkeypatch)
    cl = app.test_client()
    codes = [cl.post("/api/flag", json={"image_id": i},
                     headers={"X-Forwarded-For": "9.9.9.9"}).status_code for i in ids]
    assert 429 in codes, "must rate-limit one source"
    assert codes.count(201) <= 20


def test_public_cannot_read_flags(tmp_path, monkeypatch):
    db = tmp_path / "lib.db"
    _init_schema(db)
    vid, _ = _insert(db, subject="fysik", review_ok=None)
    app = _public_app(db, monkeypatch)
    cl = app.test_client()
    # The admin review routes are not registered on the public instance.
    assert cl.get("/api/flags").status_code == 404
    assert cl.post(f"/api/flags/{vid}/resolve", json={"status": "resolved"}).status_code in (404, 405)
    # Other mutating methods are 405 (read-only guard).
    assert cl.post("/api/jobs", json={}).status_code == 405


# ── 3. LAN review surface ────────────────────────────────────────────────────


def test_lan_review_lists_and_resolves(tmp_path, monkeypatch):
    db = tmp_path / "lib.db"
    _init_schema(db)
    vid, fp = _insert(db, subject="matematik", review_ok=None, title="Tal")
    # Seed two open flags directly (simulating two visitors) via plain sqlite3.
    import sqlite3
    _c = sqlite3.connect(str(db))
    for i, ip in enumerate(("h1", "h2")):
        _c.execute("INSERT INTO flags (id, library_id, reason, status, client_ip_hash, created_at) "
                   "VALUES (?,?,?, 'open', ?, datetime('now'))",
                   (str(uuid.uuid4()), vid, "trasig", ip))
    _c.commit(); _c.close()
    app = _private_app(db, monkeypatch)
    cl = app.test_client()
    body = cl.get("/api/flags?status=open").get_json()
    entry = next(f for f in body["flags"] if f["library_id"] == vid)
    assert entry["flag_count"] == 2
    assert entry["title"] == "Tal" and entry["subject"] == "matematik"
    assert entry["last_reason"] == "trasig"
    assert entry["thumb_url"] == "/media/" + fp
    # Resolve.
    res = cl.post(f"/api/flags/{vid}/resolve", json={"status": "resolved"}).get_json()
    assert res["updated"] == 2
    assert not any(f["library_id"] == vid for f in cl.get("/api/flags?status=open").get_json()["flags"])


# ── lb-07y: Kö management actions must be ABSENT on the public read-only instance ──


def test_ko_management_routes_absent_on_public(tmp_path, monkeypatch):
    """The owner-only Kö actions (list/cancel/retry) are double-gated off public:
    the jobs blueprint is never registered, AND the read-only guard 405s any
    mutating method. Neither cancel nor retry nor the list feed is reachable."""
    db = tmp_path / "lib.db"
    _init_schema(db)
    app = _public_app(db, monkeypatch)
    c = app.test_client()
    # the jobs blueprint isn't registered → the list route doesn't exist.
    assert c.get("/api/jobs").status_code == 404
    # the read-only guard rejects POST regardless (405), and the route is absent.
    assert c.post("/api/jobs/anything/cancel").status_code in (404, 405)
    assert c.post("/api/jobs/anything/retry").status_code in (404, 405)


def test_ko_management_routes_present_on_private(tmp_path, monkeypatch):
    """On the private LAN instance the list route IS registered (the actions are
    reachable for the owner)."""
    db = tmp_path / "lib.db"
    _init_schema(db)
    app = _private_app(db, monkeypatch)
    c = app.test_client()
    # list route exists (200, not 404) — Redis is unconfigured so the overlay is
    # best-effort; the DB read still answers with an (empty) jobs list.
    resp = c.get("/api/jobs")
    assert resp.status_code == 200
    assert "jobs" in resp.get_json()
