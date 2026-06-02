"""Tests for the skola-SVG concept-diagram baseline import (lb-svg-baseline).

Covers the three things the import rests on, with NO dependency on the live deploy
(temp DB + temp storage + inline SVG fixtures, plus opportunistic checks against the
real skola SVGs when they are present on the box):

  * the pure mapping layer (:mod:`app.svg_baseline`): normalize/stem helpers, and
    that a fysik SVG resolves to the right begrepp + AUTHORITATIVE area (exact /
    stemmed / verified-alias / forced-unmap), and the 25 teknik concepts cover the
    six larbild teknik areas;
  * the cairosvg rasterise smoke, INCLUDING the in-memory sanitiser that repairs the
    browser-lenient SVGs (raw ``<`` / ``&`` in <text>, a dangling marker ref) the
    strict renderer would otherwise reject;
  * the end-to-end import into a temp library DB + storage: rows land as
    ``image_type='diagram'`` with ``source='skola-svg-baseline'`` + ``preferred=1`` +
    a kept ``svg_source_path``, the PNG + SVG are on disk under
    ``output/<subject>/<area>/diagram/``, a re-run is idempotent (no dupes), and the
    migration-0007 columns + the preferred-first library ordering surface them.

SFW-only: every fixture + assertion is over the SFW educational concept-diagram
path; no restricted content.
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

# Import the scripts/ modules by path (scripts/ is not a package).
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

from app import svg_baseline as sb  # noqa: E402

import_skola = importlib.import_module("import_skola_svgs")  # scripts/import_skola_svgs.py

# Real skola source (present on the dev/deploy box; tests that need it skip if absent).
SKOLA_SVG_ROOT = Path(
    "/home/claudecode/gt/skola/mayor/rig/frontend/public/concept-images"
)
FYSIK_JSON = Path("/tmp/larbild-begrepp/fysik.json")

_have_skola = SKOLA_SVG_ROOT.exists() and FYSIK_JSON.exists()
skip_no_skola = pytest.mark.skipif(
    not _have_skola, reason="skola concept SVGs / fysik.json not on this box"
)

cairosvg = pytest.importorskip("cairosvg")


# ── inline fixtures (no skola dependency) ───────────────────────────────────

_GOOD_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 500" '
    'width="800" height="500">'
    '<rect width="800" height="500" fill="white"/>'
    '<text x="400" y="35" font-size="22" text-anchor="middle">Densitet</text>'
    '<circle cx="400" cy="240" r="30" fill="#fef3c7"/>'
    "</svg>"
)

# Browser-lenient SVG: a raw ``<`` and a bare ``&`` inside <text>, plus a dangling
# ``marker-end`` ref to a never-defined marker — all three crash strict cairosvg.
_MESSY_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 500" '
    'width="800" height="500">'
    '<rect width="800" height="500" fill="white"/>'
    '<text x="400" y="35" font-size="22">Tryck (p < p) & mer</text>'
    '<path d="M 100 100 L 200 200" stroke="#000" marker-end="url(#noSuchTip)"/>'
    "</svg>"
)


def _write(tmp_path: Path, rel: str, content: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ── 1) mapping layer ────────────────────────────────────────────────────────


def test_normalize_folds_swedish_accents_and_strips_parentheticals():
    assert sb.normalize("Bohr-modell (α)") == "bohr modell"
    assert sb.normalize("Växthuseffekten") == "vaxthuseffekten"
    assert sb.normalize("Ohms lag") == "ohms lag"
    assert sb.normalize(None) == ""


def test_stem_pairs_definite_plural_to_singular_without_damaging_roots():
    # plural/definite forms align with the indefinite concept name …
    assert sb.stem_phrase(sb.normalize("Isotoper")) == sb.stem_phrase(sb.normalize("Isotop"))
    assert sb.stem_phrase(sb.normalize("Dimensioner")) == sb.stem_phrase(sb.normalize("Dimension"))
    # … but short roots are NOT over-stemmed into a collision.
    assert sb.stem_phrase(sb.normalize("Neutron")) != sb.stem_phrase(sb.normalize("Newton"))
    assert sb.stem_phrase("proton") == "proton"


def test_svg_heading_reads_first_text_element(tmp_path):
    p = _write(tmp_path, "x.svg", _GOOD_SVG)
    assert sb.svg_heading(p) == "Densitet"


def test_resolve_fysik_exact_and_stem_and_area_authority():
    concepts = [
        {"begrepp": "Densitet", "area": "grundfysik"},
        {"begrepp": "Isotop", "area": "atom"},
        {"begrepp": "Tröghet", "area": "rorelse"},
    ]
    idx = sb._build_index(concepts)
    # exact filename slug
    c = sb.resolve_fysik("grundbegrepp", "densitet", "Densitet", concepts, _index=idx)
    assert c is not None and c["begrepp"] == "Densitet" and c["area"] == "grundfysik"
    # stemmed (plural filename → singular concept), area from the concept
    c = sb.resolve_fysik("atomfysik", "isotoper", "Isotoper", concepts, _index=idx)
    assert c is not None and c["begrepp"] == "Isotop" and c["area"] == "atom"


def test_resolve_fysik_verified_alias_and_forced_unmap():
    # "Radioaktiv" is the form the set stores; the alias resolves the SVG to it.
    concepts = [{"begrepp": "Radioaktiv", "area": "el-magnet"}]
    c = sb.resolve_fysik("atomfysik", "radioaktivitet", "Radioaktivitet", concepts)
    assert c is not None and c["begrepp"] == "Radioaktiv"
    # A whole-system SVG is force-unmapped (gap) even if a near word exists.
    concepts2 = [{"begrepp": "Pupill", "area": "ljus"}]
    assert sb.resolve_fysik("optik", "ogat", "Ögats uppbyggnad", concepts2) is None


def test_teknik_concepts_cover_the_six_taxonomy_areas():
    from app import taxonomy

    tax_areas = {a["slug"] for a in taxonomy.subject("teknik")["areas"]}
    used_areas = {area for _b, area in sb.TEKNIK_CONCEPTS.values()}
    assert used_areas <= tax_areas
    assert len(sb.TEKNIK_CONCEPTS) == 25
    # every teknik area slug used is a real taxonomy area
    for _b, area in sb.TEKNIK_CONCEPTS.values():
        assert area in tax_areas


# ── 2) rasterise smoke (incl. the sanitiser) ────────────────────────────────


def test_rasterise_good_svg_produces_white_png(tmp_path):
    src = _write(tmp_path, "good.svg", _GOOD_SVG)
    out = tmp_path / "good.png"
    w, h = import_skola._rasterise(src, out)
    assert out.exists() and out.stat().st_size > 0
    assert w == import_skola.RASTER_WIDTH
    # 800x500 viewBox at width 1024 → height 640.
    assert h == 640
    from PIL import Image

    with Image.open(out) as im:
        assert im.convert("RGB").getpixel((2, 2)) == (255, 255, 255)


def test_sanitiser_repairs_messy_svg_so_it_rasterises(tmp_path):
    # The raw messy SVG would raise in strict cairosvg; the import sanitises it.
    messy = _write(tmp_path, "messy.svg", _MESSY_SVG)
    out = tmp_path / "messy.png"
    w, h = import_skola._rasterise(messy, out)
    assert out.exists() and out.stat().st_size > 0 and w == import_skola.RASTER_WIDTH
    # The sanitiser is the reason — confirm it escaped the illegal chars and
    # dropped the dangling marker ref.
    fixed = import_skola._sanitise_svg(_MESSY_SVG)
    assert "p &lt; p" in fixed and "&amp; mer" in fixed
    assert "marker-end" not in fixed


# ── 3) end-to-end import (temp DB + storage) ────────────────────────────────


@pytest.fixture
def mini_svgs(tmp_path):
    """A tiny concept-image tree: one fysik SVG (maps) + one teknik SVG (creates)."""
    root = tmp_path / "concept-images"
    _write(root / "grundbegrepp", "densitet.svg", _GOOD_SVG)
    _write(
        root / "teknik_arbetssatt", "algoritm.svg",
        _GOOD_SVG.replace("Densitet", "Algoritm"),
    )
    return root


@pytest.fixture
def fysik_fixture(tmp_path):
    """A minimal fysik concept list covering the mini fysik SVG."""
    import json

    p = tmp_path / "fysik.json"
    p.write_text(json.dumps([{"begrepp": "Densitet", "area": "grundfysik"}]),
                 encoding="utf-8")
    return p


def test_import_end_to_end_writes_preferred_diagram_rows(
    tmp_path, mini_svgs, fysik_fixture
):
    db = tmp_path / "larbild.db"
    storage = tmp_path / "storage"
    os.environ["LARBILD_DB_PATH"] = str(db)
    os.environ["LARBILD_STORAGE"] = str(storage)
    try:
        summary = import_skola.run_import(
            str(mini_svgs), str(fysik_fixture), dry_run=False, force=False
        )
    finally:
        os.environ.pop("LARBILD_DB_PATH", None)
        os.environ.pop("LARBILD_STORAGE", None)

    assert summary["inserted"] == 2 and summary["errors"] == 0
    assert summary["by_subject"] == {"fysik": 1, "teknik": 1}

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM library WHERE source = ? ORDER BY subject", (import_skola.SOURCE_TAG,)
    ).fetchall()
    assert len(rows) == 2
    for r in rows:
        assert r["image_type"] == "diagram"
        assert r["preferred"] == 1
        assert r["kind"] == "bild"
        assert r["svg_source_path"] and r["svg_source_path"].endswith("__skola-svg.svg")
        assert r["file_path"].endswith("__skola-svg.png")
        # both artefacts exist on disk under the auto-sort path
        assert (storage / r["file_path"]).exists()
        assert (storage / r["svg_source_path"]).exists()
        assert r["backend"] == import_skola.BACKEND_LABEL
    # fysik row landed in the AUTHORITATIVE area (grundfysik), teknik in its area.
    by_subject = {r["subject"]: r for r in rows}
    assert by_subject["fysik"]["area"] == "grundfysik"
    assert by_subject["fysik"]["title"] == "Densitet"
    assert by_subject["teknik"]["area"] == "elektronik_styr"
    conn.close()


def test_import_is_idempotent(tmp_path, mini_svgs, fysik_fixture):
    db = tmp_path / "larbild.db"
    storage = tmp_path / "storage"
    os.environ["LARBILD_DB_PATH"] = str(db)
    os.environ["LARBILD_STORAGE"] = str(storage)
    try:
        first = import_skola.run_import(str(mini_svgs), str(fysik_fixture),
                                        dry_run=False, force=False)
        second = import_skola.run_import(str(mini_svgs), str(fysik_fixture),
                                         dry_run=False, force=False)
    finally:
        os.environ.pop("LARBILD_DB_PATH", None)
        os.environ.pop("LARBILD_STORAGE", None)

    assert first["inserted"] == 2
    assert second["inserted"] == 0 and second["exists"] == 2
    conn = sqlite3.connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM library WHERE source = ?", (import_skola.SOURCE_TAG,)
    ).fetchone()[0]
    conn.close()
    assert n == 2  # no duplicate rows on the re-run


def test_imported_rows_surface_preferred_first_in_library_api(
    tmp_path, mini_svgs, fysik_fixture
):
    from app import create_app
    from app.db import connect

    db = tmp_path / "larbild.db"
    storage = tmp_path / "storage"
    os.environ["LARBILD_DB_PATH"] = str(db)
    os.environ["LARBILD_STORAGE"] = str(storage)
    try:
        import_skola.run_import(str(mini_svgs), str(fysik_fixture),
                                dry_run=False, force=False)
        app = create_app({"TESTING": True, "DB_PATH": str(db),
                          "STORAGE_DIR": storage})
        # add a NON-preferred batch-style row for the same concept bucket
        with connect(db) as conn:
            conn.execute(
                "INSERT INTO library (id, kind, subject, area, image_type, title, "
                "file_path, backend, created_at) VALUES "
                "(?, 'bild', 'fysik', 'grundfysik', 'diagram', 'Densitet (diffusion)', "
                "?, 'vm100 · 3090', datetime('now'))",
                (str(uuid.uuid4()), f"output/fysik/grundfysik/diagram/{uuid.uuid4()}.png"),
            )
        resp = app.test_client().get(
            "/api/library/items?subject=fysik&area=grundfysik&imageType=diagram"
        )
    finally:
        os.environ.pop("LARBILD_DB_PATH", None)
        os.environ.pop("LARBILD_STORAGE", None)

    assert resp.status_code == 200
    items = resp.get_json()["items"]
    assert len(items) == 2
    # preferred (SVG baseline) sorts first; it carries source + svg_url
    first = items[0]
    assert first["preferred"] == 1
    assert first["source"] == import_skola.SOURCE_TAG
    assert first["svg_url"] and first["svg_url"].endswith("__skola-svg.svg")
    assert items[1]["preferred"] in (0, None)


# ── opportunistic full-corpus checks against the real skola SVGs ─────────────


@skip_no_skola
def test_full_corpus_maps_expected_counts():
    """The real 123 SVGs resolve to the locked coverage (regression guard)."""
    plans = list(import_skola.resolve_all(str(SKOLA_SVG_ROOT), str(FYSIK_JSON)))
    assert len(plans) == 123
    fysik = [p for p in plans if p["subject"] == "fysik"]
    teknik = [p for p in plans if p["subject"] == "teknik"]
    assert len(fysik) == 98 and len(teknik) == 25
    mapped = [p for p in plans if p["mapped"]]
    assert len(mapped) == 99            # 74 fysik + 25 teknik
    # every mapped plan has a real begrepp + a taxonomy-valid area
    from app import taxonomy

    for p in mapped:
        assert p["begrepp"]
        assert taxonomy.is_valid_area(p["subject"], p["area"]), p


@skip_no_skola
def test_committed_mapping_json_is_in_sync_with_the_generator():
    """The committed mapping JSON matches a fresh generation (no drift)."""
    import json

    build_mod = importlib.import_module("build_svg_mapping")  # scripts/build_svg_mapping.py
    fresh = build_mod.build(str(SKOLA_SVG_ROOT), str(FYSIK_JSON))
    committed = json.loads(
        (_REPO / "app" / "svg_baseline_mapping.json").read_text(encoding="utf-8")
    )
    # Compare the load-bearing parts (items + totals); ignore the prose _about.
    assert fresh["items"] == committed["items"], (
        "committed app/svg_baseline_mapping.json is stale — "
        "re-run scripts/build_svg_mapping.py"
    )
    assert fresh["totals"] == committed["totals"]
