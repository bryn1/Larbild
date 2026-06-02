"""Tests for the library items endpoint (lb-gallery).

Acceptance: GET /api/library/items?subject=&area=[&imageType=] returns the
non-deleted, non-dangling ``library`` rows for that node, newest first, each
carrying a servable image URL (``url`` → ``/media/<file_path>``) plus the
provenance the gallery + full-size view render. Soft-deleted and file-missing
(``missing_since``) rows are excluded; an unknown taxonomy node is an empty 200;
a missing required param is a 400.

This is the data source that makes generated images *visible* in the Bibliotek
(the count tree from /api/library has no items) — so the headline assertion is
that every returned item has a real, /media-prefixed URL.
"""
from __future__ import annotations

import uuid

from app import create_app
from app.db import connect


def _app(db_path):
    return create_app({"TESTING": True, "DB_PATH": db_path})


def _insert(db_path, *, subject, area, image_type="diagram", kind="bild",
            file_path=None, deleted=False, missing=False, title="Bild",
            prompt="en SFW illustration", lora="larbild_sfw", seed=42,
            backend="vm100 · 3090", created_at=None):
    rid = str(uuid.uuid4())
    fp = file_path or f"output/{subject}/{area}/{image_type}/{rid}.png"
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO library
                (id, kind, subject, area, image_type, title, file_path, prompt,
                 lora, seed, backend, width, height, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
            """,
            (rid, kind, subject, area, image_type, title, fp, prompt, lora, seed,
             backend, 1024, 768, created_at),
        )
        if deleted:
            conn.execute("UPDATE library SET deleted_at = datetime('now') WHERE id = ?", (rid,))
        if missing:
            conn.execute("UPDATE library SET missing_since = datetime('now') WHERE id = ?", (rid,))
    return rid, fp


# ── shape + servable URL ────────────────────────────────────────────────────


def test_items_returns_rows_with_servable_urls(tmp_path):
    db = tmp_path / "lib.db"
    app = _app(db)
    rid, fp = _insert(db, subject="fysik", area="rorelse", image_type="diagram")

    resp = app.test_client().get("/api/library/items?subject=fysik&area=rorelse")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["subject"] == "fysik" and body["area"] == "rorelse"
    assert body["count"] == 1
    item = body["items"][0]
    assert item["id"] == rid
    # The load-bearing field: a servable, /media-prefixed URL for the <img>.
    assert item["url"] == "/media/" + fp
    assert item["url"].startswith("/media/output/fysik/rorelse/")
    # thumb_url falls back to the full image when no thumb exists.
    assert item["thumb_url"] == item["url"]
    # provenance the gallery/full-size view show
    for key in ("title", "prompt", "lora", "seed", "backend", "width", "height",
                "area_name", "generated_at", "kind"):
        assert key in item, f"item missing {key}"
    # area_name resolves from the taxonomy SoT, not the bare slug.
    assert item["area_name"] and item["area_name"] != "rorelse"


def test_items_uses_thumb_url_when_thumb_present(tmp_path):
    db = tmp_path / "lib.db"
    app = _app(db)
    rid, fp = _insert(db, subject="kemi", area="materia")
    with connect(db) as conn:
        conn.execute("UPDATE library SET thumb_path = ? WHERE id = ?",
                     ("output/kemi/materia/diagram/thumb.png", rid))
    item = app.test_client().get("/api/library/items?subject=kemi&area=materia").get_json()["items"][0]
    assert item["thumb_url"] == "/media/output/kemi/materia/diagram/thumb.png"
    assert item["url"] == "/media/" + fp  # full-size stays the original


def test_items_url_is_percent_encoded(tmp_path):
    # A space/odd char in a filename must not break the <img> URL; slashes kept.
    db = tmp_path / "lib.db"
    app = _app(db)
    _insert(db, subject="fysik", area="ljud",
            file_path="output/fysik/ljud/diagram/lärbild bild.png")
    item = app.test_client().get("/api/library/items?subject=fysik&area=ljud").get_json()["items"][0]
    assert item["url"].startswith("/media/output/fysik/ljud/diagram/")
    assert " " not in item["url"]  # the space is encoded
    assert item["url"].count("/media/output/fysik/ljud/diagram/") == 1


# ── scoping ─────────────────────────────────────────────────────────────────


def test_items_scoped_to_subject_and_area(tmp_path):
    db = tmp_path / "lib.db"
    app = _app(db)
    _insert(db, subject="fysik", area="rorelse")
    _insert(db, subject="fysik", area="ljud")   # different area
    _insert(db, subject="kemi", area="materia")  # different subject

    body = app.test_client().get("/api/library/items?subject=fysik&area=rorelse").get_json()
    assert body["count"] == 1
    assert all(it["area"] == "rorelse" and it["subject"] == "fysik" for it in body["items"])


def test_items_filtered_by_image_type(tmp_path):
    db = tmp_path / "lib.db"
    app = _app(db)
    _insert(db, subject="fysik", area="rorelse", image_type="diagram")
    _insert(db, subject="fysik", area="rorelse", image_type="foto")

    body = app.test_client().get(
        "/api/library/items?subject=fysik&area=rorelse&imageType=diagram"
    ).get_json()
    assert body["image_type"] == "diagram"
    assert body["count"] == 1
    assert body["items"][0]["image_type"] == "diagram"


def test_items_newest_first(tmp_path):
    db = tmp_path / "lib.db"
    app = _app(db)
    _insert(db, subject="fysik", area="ljus", title="old", created_at="2026-01-01 00:00:00")
    _insert(db, subject="fysik", area="ljus", title="new", created_at="2026-06-01 00:00:00")

    items = app.test_client().get("/api/library/items?subject=fysik&area=ljus").get_json()["items"]
    assert [it["title"] for it in items] == ["new", "old"]


# ── exclusions ──────────────────────────────────────────────────────────────


def test_items_excludes_soft_deleted(tmp_path):
    db = tmp_path / "lib.db"
    app = _app(db)
    _insert(db, subject="fysik", area="atom", title="live")
    _insert(db, subject="fysik", area="atom", title="gone", deleted=True)

    items = app.test_client().get("/api/library/items?subject=fysik&area=atom").get_json()["items"]
    assert [it["title"] for it in items] == ["live"]


def test_items_excludes_dangling_missing_since(tmp_path):
    # A row whose file vanished (reconcile flagged missing_since) must not render
    # a broken tile — it's excluded from the gallery.
    db = tmp_path / "lib.db"
    app = _app(db)
    _insert(db, subject="fysik", area="universum", title="present")
    _insert(db, subject="fysik", area="universum", title="missing", missing=True)

    items = app.test_client().get("/api/library/items?subject=fysik&area=universum").get_json()["items"]
    assert [it["title"] for it in items] == ["present"]


def test_film_item_returned_with_url(tmp_path):
    db = tmp_path / "lib.db"
    app = _app(db)
    rid, fp = _insert(db, subject="biologi", area="celler", kind="film",
                      image_type=None, file_path="output/biologi/celler/film/clip.mp4")
    item = app.test_client().get("/api/library/items?subject=biologi&area=celler").get_json()["items"][0]
    assert item["kind"] == "film"
    assert item["url"] == "/media/" + fp


# ── validation / empties ────────────────────────────────────────────────────


def test_missing_required_params_is_400(tmp_path):
    app = _app(tmp_path / "lib.db")
    c = app.test_client()
    assert c.get("/api/library/items").status_code == 400
    assert c.get("/api/library/items?subject=fysik").status_code == 400
    assert c.get("/api/library/items?area=rorelse").status_code == 400


def test_unknown_node_is_empty_200(tmp_path):
    # An unknown (taxonomy-invalid) subject/area pair → empty gallery, not error.
    app = _app(tmp_path / "lib.db")
    resp = app.test_client().get("/api/library/items?subject=nope&area=nope")
    assert resp.status_code == 200
    assert resp.get_json()["items"] == []


def test_valid_but_empty_node_is_empty_200(tmp_path):
    app = _app(tmp_path / "lib.db")
    resp = app.test_client().get("/api/library/items?subject=fysik&area=rorelse")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["items"] == [] and body["count"] == 0
