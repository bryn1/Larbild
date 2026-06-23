"""Tests for migration 0010 — reconcile matte-svg-gen rows onto matematik areas.

Acceptance (lb-1gi): the 104 ``source = 'matte-svg-gen'`` rows were imported
under granular ``ak7-*/ak8-*/ak9-*`` concept slugs in ``area``, so they never
joined onto the matematik taxonomy nav tree (which keys on the coarse area
slugs). The migration remaps each row's ``area`` to the one coarse matematik
taxonomy area it belongs to, mirroring how every other asset is stored — so the
rows become browsable in the matematik nav — while leaving ``file_path`` (the
storage/serving key) and other subjects' rows untouched, and is idempotent.

The migration normally runs once at boot, *before* any matte-svg-gen rows are
queried; these tests seed the granular rows first, then invoke the migration's
``up(conn)`` directly (the runner's own loader names the NNNN_-file module), so
they exercise the real apply path against pre-existing data.
"""
from __future__ import annotations

import uuid
from importlib import util as _import_util
from pathlib import Path

from app import taxonomy
from app.db import connect, init_db

_MIG_PATH = (
    Path(taxonomy.__file__).resolve().parents[1] / "migrations" / "0010_matte_area_reconcile.py"
)


def _load_migration():
    """Load the NNNN_-named migration module (not a valid Python identifier)."""
    spec = _import_util.spec_from_file_location("_lb_mig_0010", _MIG_PATH)
    mod = _import_util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _insert(conn, *, subject, area, source, title="T"):
    rid = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO library (id, kind, subject, area, image_type, title, file_path, source)
        VALUES (?, 'bild', ?, ?, 'diagram', ?, ?, ?)
        """,
        (rid, subject, area, title, f"output/{subject}/{area}/diagram/{rid}.png", source),
    )
    return rid


def test_all_mapping_targets_are_valid_matematik_areas():
    valid = set(taxonomy.area_slugs("matematik"))
    targets = set(_load_migration().AREA_MAP.values())
    assert targets <= valid, f"targets not in taxonomy SoT: {sorted(targets - valid)}"


def test_remaps_every_matte_row_to_coarse_area(tmp_path):
    db = tmp_path / "remap.db"
    init_db(db)
    mig = _load_migration()
    valid = set(taxonomy.area_slugs("matematik"))
    with connect(db) as conn:
        ids = {
            slug: _insert(conn, subject="matematik", area=slug, source="matte-svg-gen")
            for slug in mig.AREA_MAP
        }
        mig.up(conn)
        for slug, rid in ids.items():
            area = conn.execute("SELECT area FROM library WHERE id=?", (rid,)).fetchone()[0]
            assert area == mig.AREA_MAP[slug], f"{slug} -> {area}, want {mig.AREA_MAP[slug]}"
            assert area in valid


def test_file_path_is_preserved(tmp_path):
    db = tmp_path / "fp.db"
    init_db(db)
    mig = _load_migration()
    with connect(db) as conn:
        rid = _insert(conn, subject="matematik", area="ak8-volym", source="matte-svg-gen")
        mig.up(conn)
        row = conn.execute("SELECT area, file_path FROM library WHERE id=?", (rid,)).fetchone()
    assert row[0] == "geometri"  # ak8-volym -> geometri
    assert row[1] == f"output/matematik/ak8-volym/diagram/{rid}.png"  # untouched


def test_does_not_touch_other_subjects_or_sources(tmp_path):
    db = tmp_path / "iso.db"
    init_db(db)
    mig = _load_migration()
    with connect(db) as conn:
        fys = _insert(conn, subject="fysik", area="rorelse", source="svg-baseline")
        skola = _insert(conn, subject="matematik", area="geometri", source="skola-diagram")
        mig.up(conn)
        a_fys = conn.execute("SELECT area FROM library WHERE id=?", (fys,)).fetchone()[0]
        a_skola = conn.execute("SELECT area FROM library WHERE id=?", (skola,)).fetchone()[0]
    assert a_fys == "rorelse"
    assert a_skola == "geometri"


def test_idempotent(tmp_path):
    db = tmp_path / "idem.db"
    init_db(db)
    mig = _load_migration()
    with connect(db) as conn:
        rid = _insert(conn, subject="matematik", area="ak7-vinklar", source="matte-svg-gen")
        mig.up(conn)
        first = conn.execute("SELECT area FROM library WHERE id=?", (rid,)).fetchone()[0]
        mig.up(conn)  # second apply: now a coarse slug, not in AREA_MAP → no-op
        second = conn.execute("SELECT area FROM library WHERE id=?", (rid,)).fetchone()[0]
    assert first == "geometri"
    assert second == "geometri"
