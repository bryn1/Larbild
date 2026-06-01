"""Tests for the library listing endpoint (lb-5rc).

Acceptance: GET /api/library returns the full subject/area tree with central
content + per-node asset counts (0 when none generated yet); the shape matches
what the Bibliotek view (lb-eee) needs; counts roll up from the library table.

The tree structure is asserted against the taxonomy SoT counts (17 subjects,
102 areas), and the count behaviour is exercised by booting a fresh temp SQLite
via the migrations runner, inserting library rows, and asserting the affected
node + its subject ancestor increment while every other node stays 0.
"""
from __future__ import annotations

import uuid

from app import create_app
from app import taxonomy
from app.db import connect, init_db


def _app(db_path):
    """A test app whose DB is the given temp path (migrations run on boot)."""
    return create_app({"TESTING": True, "DB_PATH": db_path})


def _insert_library_row(db_path, *, subject, area, image_type="illustration", kind="bild"):
    """Insert one non-deleted library row for the given taxonomy node."""
    rid = str(uuid.uuid4())
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO library (id, kind, subject, area, image_type, title, file_path)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (rid, kind, subject, area, image_type, "T", f"{subject}/{area}/{rid}.png"),
        )
    return rid


# ── shape + structure: the full taxonomy tree, counts default 0 ────────────


def test_library_returns_full_taxonomy_tree(tmp_path):
    app = _app(tmp_path / "lib.db")
    resp = app.test_client().get("/api/library")
    assert resp.status_code == 200
    assert resp.is_json
    body = resp.get_json()

    # 17 subjects, 102 areas total — the canonical LGR22 shape from the SoT.
    assert len(body["subjects"]) == 17
    assert sum(len(s["areas"]) for s in body["subjects"]) == 102


def test_top_level_shape_matches_contract(tmp_path):
    app = _app(tmp_path / "lib.db")
    body = app.test_client().get("/api/library").get_json()

    assert "subjects" in body and "generated_at" in body
    subj = body["subjects"][0]
    # subject node keys the Bibliotek view consumes
    for key in ("id", "name", "asset_count", "areas"):
        assert key in subj, f"subject missing {key}"
    area = subj["areas"][0]
    for key in ("id", "name", "asset_count", "central_content"):
        assert key in area, f"area missing {key}"
    entry = area["central_content"][0]
    for key in ("id", "text", "asset_count"):
        assert key in entry, f"central_content entry missing {key}"


def test_node_ids_are_stable_taxonomy_slugs(tmp_path):
    # Reference nodes by id (slug), never free text (risk R7). ids must equal the
    # taxonomy slugs so downstream lb-eee / lb-b1r can key on them.
    app = _app(tmp_path / "lib.db")
    body = app.test_client().get("/api/library").get_json()
    by_id = {s["id"]: s for s in body["subjects"]}
    assert set(by_id) == set(taxonomy.subject_slugs())
    fysik = by_id["fysik"]
    area_ids = {a["id"] for a in fysik["areas"]}
    assert area_ids == set(taxonomy.area_slugs("fysik"))


def test_central_content_non_empty_and_ids_stable(tmp_path):
    app = _app(tmp_path / "lib.db")
    body = app.test_client().get("/api/library").get_json()
    for s in body["subjects"]:
        for a in s["areas"]:
            cc = a["central_content"]
            assert cc, f"area {s['id']}:{a['id']} has empty central_content"
            for n, entry in enumerate(cc, start=1):
                # deterministic, position-derived id
                assert entry["id"] == f"{s['id']}:{a['id']}:c{n}"
                assert entry["text"]


def test_central_content_splits_topics(tmp_path):
    # 'Bråk, decimaltal, procent, potenser, grundpotensform.' → 5 topic entries,
    # trailing period stripped, each a clean topic string.
    app = _app(tmp_path / "lib.db")
    body = app.test_client().get("/api/library").get_json()
    matematik = next(s for s in body["subjects"] if s["id"] == "matematik")
    taluppfattning = next(a for a in matematik["areas"] if a["id"] == "taluppfattning")
    texts = [e["text"] for e in taluppfattning["central_content"]]
    assert texts == ["Bråk", "decimaltal", "procent", "potenser", "grundpotensform"]
    assert all(not t.endswith(".") for t in texts)


def test_all_counts_zero_on_empty_db(tmp_path):
    app = _app(tmp_path / "lib.db")
    body = app.test_client().get("/api/library").get_json()
    for s in body["subjects"]:
        assert s["asset_count"] == 0
        for a in s["areas"]:
            assert a["asset_count"] == 0
            assert all(e["asset_count"] == 0 for e in a["central_content"])


# ── counts roll up from the library table ──────────────────────────────────


def test_count_increments_node_and_subject_ancestor(tmp_path):
    db = tmp_path / "lib.db"
    app = _app(db)
    # fysik area 'ljud' exists in the taxonomy (subject-scoped).
    assert taxonomy.is_valid_area("fysik", "ljud")
    _insert_library_row(db, subject="fysik", area="ljud")

    body = app.test_client().get("/api/library").get_json()
    fysik = next(s for s in body["subjects"] if s["id"] == "fysik")
    ljud = next(a for a in fysik["areas"] if a["id"] == "ljud")

    assert ljud["asset_count"] == 1            # the node
    assert fysik["asset_count"] == 1           # its subject ancestor

    # every other subject still 0; every other fysik area still 0
    for s in body["subjects"]:
        if s["id"] != "fysik":
            assert s["asset_count"] == 0
    for a in fysik["areas"]:
        if a["id"] != "ljud":
            assert a["asset_count"] == 0


def test_subject_total_sums_multiple_areas(tmp_path):
    db = tmp_path / "lib.db"
    app = _app(db)
    _insert_library_row(db, subject="fysik", area="ljud")
    _insert_library_row(db, subject="fysik", area="ljud")  # 2 in ljud
    _insert_library_row(db, subject="fysik", area="ljus")  # 1 in ljus

    body = app.test_client().get("/api/library").get_json()
    fysik = next(s for s in body["subjects"] if s["id"] == "fysik")
    counts = {a["id"]: a["asset_count"] for a in fysik["areas"]}
    assert counts["ljud"] == 2
    assert counts["ljus"] == 1
    assert fysik["asset_count"] == 3  # rolled up


def test_soft_deleted_rows_excluded(tmp_path):
    db = tmp_path / "lib.db"
    app = _app(db)
    rid = _insert_library_row(db, subject="kemi", area="vatten")
    # soft-delete it
    with connect(db) as conn:
        conn.execute(
            "UPDATE library SET deleted_at = datetime('now') WHERE id = ?", (rid,)
        )

    body = app.test_client().get("/api/library").get_json()
    kemi = next(s for s in body["subjects"] if s["id"] == "kemi")
    assert kemi["asset_count"] == 0  # the soft-deleted row is not counted


def test_film_rows_count_too(tmp_path):
    # A 'film' artefact carries subject/area like a 'bild' and must count.
    db = tmp_path / "lib.db"
    app = _app(db)
    _insert_library_row(db, subject="biologi", area="celler", kind="film", image_type=None)

    body = app.test_client().get("/api/library").get_json()
    biologi = next(s for s in body["subjects"] if s["id"] == "biologi")
    celler = next(a for a in biologi["areas"] if a["id"] == "celler")
    assert celler["asset_count"] == 1


def test_generated_at_is_iso_utc(tmp_path):
    app = _app(tmp_path / "lib.db")
    body = app.test_client().get("/api/library").get_json()
    # parseable ISO-8601; UTC offset present
    from datetime import datetime

    dt = datetime.fromisoformat(body["generated_at"])
    assert dt.utcoffset() is not None
