"""Tests for the asset-lessons API (lb-zdp).

A *lesson* here is a teacher-curated, ordered collection of EXISTING library
assets — a SEPARATE concern from the lb-lesson video-manuscript funnel. These
tests exercise the full CRUD + membership + reorder surface and assert the asset
shape reuses the library item contract (servable /media URL).
"""
from __future__ import annotations

import uuid

from app import create_app
from app.db import connect


def _app(db_path):
    return create_app({"TESTING": True, "DB_PATH": db_path})


def _insert_asset(db_path, *, subject="fysik", area="ljus", image_type="diagram",
                  kind="bild", deleted=False, missing=False, title="Bild"):
    rid = str(uuid.uuid4())
    fp = f"output/{subject}/{area}/{image_type}/{rid}.png"
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO library
                (id, kind, subject, area, image_type, title, file_path,
                 width, height, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (rid, kind, subject, area, image_type, title, fp, 1024, 768),
        )
        if deleted:
            conn.execute("UPDATE library SET deleted_at = datetime('now') WHERE id = ?", (rid,))
        if missing:
            conn.execute("UPDATE library SET missing_since = datetime('now') WHERE id = ?", (rid,))
    return rid, fp


# ── create + list ───────────────────────────────────────────────────────────


def test_create_and_list_lesson(tmp_path):
    db = tmp_path / "lb.db"
    client = _app(db).test_client()

    resp = client.post("/api/asset-lessons", json={"title": "Ljud åk 8", "subject": "fysik", "area": "ljud"})
    assert resp.status_code == 201
    lesson = resp.get_json()["lesson"]
    assert lesson["title"] == "Ljud åk 8"
    assert lesson["subject"] == "fysik" and lesson["area"] == "ljud"
    assert lesson["asset_count"] == 0
    assert lesson["thumb_urls"] == []

    listing = client.get("/api/asset-lessons").get_json()
    assert listing["count"] == 1
    assert listing["lessons"][0]["id"] == lesson["id"]


def test_create_requires_title(tmp_path):
    db = tmp_path / "lb.db"
    client = _app(db).test_client()
    assert client.post("/api/asset-lessons", json={}).status_code == 400
    assert client.post("/api/asset-lessons", json={"title": "   "}).status_code == 400


# ── membership: add / dedupe / remove ───────────────────────────────────────


def test_add_assets_dedupe_and_serializes_with_media_url(tmp_path):
    db = tmp_path / "lb.db"
    client = _app(db).test_client()
    lid = client.post("/api/asset-lessons", json={"title": "L"}).get_json()["lesson"]["id"]
    a1, fp1 = _insert_asset(db)
    a2, _ = _insert_asset(db)

    assert client.post(f"/api/asset-lessons/{lid}/items", json={"library_id": a1}).status_code == 200
    r = client.post(f"/api/asset-lessons/{lid}/items", json={"library_id": a2})
    assert r.status_code == 200 and r.get_json()["asset_count"] == 2
    # Re-adding a1 is a NO-OP (idempotent), not a duplicate.
    dup = client.post(f"/api/asset-lessons/{lid}/items", json={"library_id": a1})
    assert dup.status_code == 200 and dup.get_json()["asset_count"] == 2

    got = client.get(f"/api/asset-lessons/{lid}").get_json()
    assert got["lesson"]["asset_count"] == 2
    ids = [it["id"] for it in got["items"]]
    assert ids == [a1, a2]  # insertion order preserved
    # The asset reuses the library item contract: a servable /media URL.
    assert got["items"][0]["url"] == "/media/" + fp1


def test_add_missing_asset_or_lesson_404(tmp_path):
    db = tmp_path / "lb.db"
    client = _app(db).test_client()
    lid = client.post("/api/asset-lessons", json={"title": "L"}).get_json()["lesson"]["id"]
    assert client.post(f"/api/asset-lessons/{lid}/items", json={"library_id": "nope"}).status_code == 404
    assert client.post("/api/asset-lessons/nope/items", json={"library_id": "x"}).status_code == 404
    assert client.post(f"/api/asset-lessons/{lid}/items", json={}).status_code == 400


def test_remove_asset(tmp_path):
    db = tmp_path / "lb.db"
    client = _app(db).test_client()
    lid = client.post("/api/asset-lessons", json={"title": "L"}).get_json()["lesson"]["id"]
    a1, _ = _insert_asset(db)
    client.post(f"/api/asset-lessons/{lid}/items", json={"library_id": a1})
    r = client.delete(f"/api/asset-lessons/{lid}/items/{a1}")
    assert r.status_code == 200 and r.get_json()["asset_count"] == 0
    # The library asset itself still exists (lesson removal != asset delete).
    with connect(db) as conn:
        assert conn.execute("SELECT 1 FROM library WHERE id = ?", (a1,)).fetchone() is not None


def test_deleted_and_missing_assets_excluded(tmp_path):
    db = tmp_path / "lb.db"
    client = _app(db).test_client()
    lid = client.post("/api/asset-lessons", json={"title": "L"}).get_json()["lesson"]["id"]
    live, _ = _insert_asset(db)
    gone, _ = _insert_asset(db)  # will be soft-deleted after add
    client.post(f"/api/asset-lessons/{lid}/items", json={"library_id": live})
    client.post(f"/api/asset-lessons/{lid}/items", json={"library_id": gone})
    with connect(db) as conn:
        conn.execute("UPDATE library SET deleted_at = datetime('now') WHERE id = ?", (gone,))
    got = client.get(f"/api/asset-lessons/{lid}").get_json()
    assert [it["id"] for it in got["items"]] == [live]


# ── reorder ─────────────────────────────────────────────────────────────────


def test_reorder(tmp_path):
    db = tmp_path / "lb.db"
    client = _app(db).test_client()
    lid = client.post("/api/asset-lessons", json={"title": "L"}).get_json()["lesson"]["id"]
    a1, _ = _insert_asset(db); a2, _ = _insert_asset(db); a3, _ = _insert_asset(db)
    for a in (a1, a2, a3):
        client.post(f"/api/asset-lessons/{lid}/items", json={"library_id": a})
    r = client.put(f"/api/asset-lessons/{lid}/order", json={"order": [a3, a1, a2]})
    assert r.status_code == 200
    assert [it["id"] for it in r.get_json()["items"]] == [a3, a1, a2]
    # A partial order keeps unlisted members after the listed ones.
    r2 = client.put(f"/api/asset-lessons/{lid}/order", json={"order": [a2]})
    assert [it["id"] for it in r2.get_json()["items"]][0] == a2
    assert r.status_code == 200


def test_reorder_bad_body_400(tmp_path):
    db = tmp_path / "lb.db"
    client = _app(db).test_client()
    lid = client.post("/api/asset-lessons", json={"title": "L"}).get_json()["lesson"]["id"]
    assert client.put(f"/api/asset-lessons/{lid}/order", json={"order": "nope"}).status_code == 400


# ── rename / delete ─────────────────────────────────────────────────────────


def test_rename_and_delete(tmp_path):
    db = tmp_path / "lb.db"
    client = _app(db).test_client()
    lid = client.post("/api/asset-lessons", json={"title": "Old"}).get_json()["lesson"]["id"]
    r = client.patch(f"/api/asset-lessons/{lid}", json={"title": "New"})
    assert r.status_code == 200 and r.get_json()["lesson"]["title"] == "New"
    assert client.delete(f"/api/asset-lessons/{lid}").status_code == 200
    assert client.get(f"/api/asset-lessons/{lid}").status_code == 404


def test_delete_cascades_membership_not_assets(tmp_path):
    db = tmp_path / "lb.db"
    client = _app(db).test_client()
    lid = client.post("/api/asset-lessons", json={"title": "L"}).get_json()["lesson"]["id"]
    a1, _ = _insert_asset(db)
    client.post(f"/api/asset-lessons/{lid}/items", json={"library_id": a1})
    client.delete(f"/api/asset-lessons/{lid}")
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM asset_lesson_items").fetchone()[0] == 0
        assert conn.execute("SELECT 1 FROM library WHERE id = ?", (a1,)).fetchone() is not None
