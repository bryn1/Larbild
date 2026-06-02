#!/usr/bin/env python3
"""Import the 123 Claude-authored skola concept SVGs as the Lärbild DIAGRAM baseline.

The skola rig authored 123 exact, perfectly-Swedish-labelled vector concept
diagrams. For these structured fysik/teknik concepts a hand-authored vector is
strictly better than a diffusion render or a matplotlib auto-diagram, so each is
imported into the Lärbild library as the concept's ``diagram`` baseline and flagged
the **preferred** version. The original ``.svg`` is kept on disk alongside a
rasterised PNG so it can be served / re-rasterised as true vector later.

What it does, per mapped SVG (see :mod:`app.svg_baseline` for the mapping):

  1. Resolve subject + area (fysik: from the matched concept's authoritative
     ``area``; teknik: from the created-concept table).
  2. Copy the SVG verbatim into storage at
     ``output/<subject>/<area>/diagram/<name>__skola-svg.svg`` (the kept vector).
  3. Rasterise a white-background PNG (~1024px wide) at
     ``output/<subject>/<area>/diagram/<name>__skola-svg.png`` (the gallery image).
  4. INSERT a ``library`` row: kind='bild', image_type='diagram', file_path = the
     PNG, svg_source_path = the SVG, source='skola-svg-baseline', preferred=1,
     backend='skola-svg · vector', prompt = the SVG heading (provenance), title =
     the begrepp. ``workflow_json`` records the source svg + heading so an audit /
     re-rasterise can replay it.

IDEMPOTENT + NON-DESTRUCTIVE + batch-safe:
  * Filenames carry a distinct ``__skola-svg`` suffix, so a row NEVER collides with
    the running render batch's ``UNIQUE(file_path)`` rows — an imported SVG baseline
    and a batch-rendered diffusion asset for the same concept COEXIST; ``preferred=1``
    + the review loop resolve which to surface. The running batch is not touched.
  * Re-running skips any SVG already imported (matched by its deterministic
    file_path), so a second run is a no-op. ``--force`` re-imports (re-rasterise +
    upsert) — still by the same stable path, so it updates in place, never dupes.
  * The skola source SVGs are READ ONLY (copied, never moved/deleted).

Reads ``LARBILD_DB_PATH`` + ``LARBILD_STORAGE`` from the env (the same vars the
service uses), so on the deploy host it writes straight into the live library +
storage. ``--dry-run`` resolves + reports without writing.

A SECOND vector lane lives here too: ``--authored`` imports brand-new
Claude-authored concept diagrams for GAP concepts the skola corpus never covered
(the ~372-concept fysik gap). They share this module's rasteriser, sanitiser,
``_upsert_row`` and preferred-first treatment, but carry their own provenance
(``source='authored-svg'``, ``__authored-svg`` file suffix) so an authored
baseline, a skola baseline, and a batch render for one concept all coexist. The
authored lane is manifest-driven (``assets/authored_svgs/fysik/manifest.json``
maps ``<area>/<slug>`` → canonical begrepp + display title) and cross-checks each
(begrepp, area) against fysik.json so an authored SVG can't land in the wrong
Bibliotek node.

Usage:
    # skola baseline lane (the original 123 SVGs)
    LARBILD_DB_PATH=.../larbild.db LARBILD_STORAGE=.../larbild \
      python3 scripts/import_skola_svgs.py \
        [--svg-root /path/to/concept-images] \
        [--fysik-json /tmp/larbild-begrepp/fysik.json] \
        [--dry-run] [--force]

    # Claude-authored gap-fill lane
    LARBILD_DB_PATH=.../larbild.db LARBILD_STORAGE=.../larbild \
      python3 scripts/import_skola_svgs.py --authored \
        [--authored-root assets/authored_svgs/fysik] \
        [--authored-manifest assets/authored_svgs/fysik/manifest.json] \
        [--fysik-json /tmp/larbild-begrepp/fysik.json] \
        [--dry-run] [--force]

SFW-only: educational concept diagrams into the SFW library; no restricted content.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

# Run from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import svg_baseline as sb  # noqa: E402

DEFAULT_SVG_ROOT = "/home/claudecode/gt/skola/mayor/rig/frontend/public/concept-images"

# Provenance tag written to library.source (the migration-0007 column). The
# review loop / gallery read it to know a row is a hand-authored vector baseline.
SOURCE_TAG = "skola-svg-baseline"
# Backend label — honest about HOW this image was produced (a kept vector,
# rasterised on CPU), mirroring render_worker's "procedural · CPU" convention.
BACKEND_LABEL = "skola-svg · vector"
# Raster width for the gallery PNG. viewBox is 800x500, so 1024 wide → 640 tall.
RASTER_WIDTH = 1024
# The library bucket: these are all diagrams.
IMAGE_TYPE = "diagram"
# Distinct filename suffix so we never collide with a batch render's file_path.
SUFFIX = "__skola-svg"

# ── Claude-authored-SVG gap-fill lane (lb-authored-svg) ─────────────────────
# A SECOND vector lane: Claude authors brand-new concept diagrams for gap concepts
# the skola corpus never covered (the 372-concept fysik gap). They share the same
# house style, rasteriser, sanitiser, upsert + preferred-first treatment as the
# skola baseline, but carry their OWN provenance so the two are distinguishable.
AUTHORED_SOURCE_TAG = "authored-svg"
AUTHORED_BACKEND_LABEL = "authored-svg · vector"
# Distinct suffix → its own deterministic file_path, so an authored baseline,
# a skola baseline, AND a batch render for one concept can all coexist.
AUTHORED_SUFFIX = "__authored-svg"
# Where the authored sources live in the repo + the manifest mapping each
# ``<area>/<slug>`` → its canonical begrepp + display title.
DEFAULT_AUTHORED_ROOT = "assets/authored_svgs/fysik"
DEFAULT_AUTHORED_MANIFEST = "assets/authored_svgs/fysik/manifest.json"


class ImportError_(RuntimeError):
    """An SVG could not be imported (rasterise / copy / DB failure)."""


def _rel_paths(subject: str, area: str, name: str, suffix: str = SUFFIX) -> tuple[Path, Path]:
    """STORAGE_DIR-relative (png_path, svg_path) for one concept's baseline.

    Both live under ``output/<subject>/<area>/diagram/`` with a distinct ``suffix``
    (``__skola-svg`` for the skola baseline, ``__authored-svg`` for the authored
    gap-fill lane) so they sort into the right Bibliotek node and never clash with
    a batch-rendered asset's path — or with each other.
    """
    base = Path("output") / subject / area / IMAGE_TYPE
    return base / f"{name}{suffix}.png", base / f"{name}{suffix}.svg"


# A bare ``&`` that does NOT begin a valid XML/HTML entity (the skola SVGs were
# authored browser-lenient and a few have raw ``&`` / ``<`` inside <text>).
_BARE_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")
_TEXT_EL = re.compile(r"(<text\b[^>]*>)(.*?)(</text>)", re.S)
# A marker-{start,mid,end}="url(#id)" presentation attribute (and the id it points
# at) — a few SVGs reference an arrowhead marker that was never <marker>-defined,
# which crashes cairosvg's strict renderer.
_MARKER_ATTR = re.compile(r'\s*marker-(?:start|mid|end)\s*=\s*"url\(#([^)"]+)\)"')


def _sanitise_svg(svg_text: str) -> str:
    """Repair browser-lenient SVG so cairosvg's strict parser/renderer accepts it.

    NON-DESTRUCTIVE to the source on disk — this only repairs the in-memory copy
    fed to the rasteriser; the verbatim ``.svg`` is still copied unchanged into
    storage (the true vector). Three repairs, each a no-op when not needed:

      1. escape a bare ``&`` not starting a valid entity → ``&amp;``;
      2. escape stray ``<`` / ``>`` INSIDE ``<text>…</text>`` content (e.g.
         ``p < p`` / ``<0.01 nm``) → ``&lt;`` / ``&gt;`` (markup is untouched —
         only the text node body is escaped);
      3. drop a ``marker-*="url(#id)"`` attribute whose ``#id`` has no matching
         ``<marker id="id">`` definition (a dangling arrowhead ref → cairosvg
         ``AttributeError``); the path still renders, just without the tiny tip.
    """
    # 1) bare ampersands anywhere.
    svg_text = _BARE_AMP.sub("&amp;", svg_text)

    # 2) stray <, > inside text element bodies only.
    def _fix_text(m: "re.Match[str]") -> str:
        body = m.group(2).replace("<", "&lt;").replace(">", "&gt;")
        return m.group(1) + body + m.group(3)

    svg_text = _TEXT_EL.sub(_fix_text, svg_text)

    # 3) strip dangling marker refs (id not defined by a <marker id=...>).
    defined = set(re.findall(r'<marker\b[^>]*\bid\s*=\s*"([^"]+)"', svg_text))

    def _fix_marker(m: "re.Match[str]") -> str:
        return "" if m.group(1) not in defined else m.group(0)

    svg_text = _MARKER_ATTR.sub(_fix_marker, svg_text)
    return svg_text


def _rasterise(svg_path: Path, dest_png: Path) -> tuple[int, int]:
    """Rasterise ``svg_path`` → ``dest_png`` (white bg, RASTER_WIDTH wide).

    Returns (width, height). Imports cairosvg lazily so the module imports on a box
    without it (the test that doesn't rasterise still runs). White background is
    forced (``background_color='white'``) so a transparent SVG region renders as the
    läroboksstil white page, matching the source's intent. The SVG is sanitised
    in-memory first (:func:`_sanitise_svg`) so a browser-lenient source still
    rasterises; the on-disk source copy is left verbatim.
    """
    import cairosvg  # lazy: only needed when actually writing

    dest_png.parent.mkdir(parents=True, exist_ok=True)
    svg_text = svg_path.read_text(encoding="utf-8")
    cairosvg.svg2png(
        bytestring=_sanitise_svg(svg_text).encode("utf-8"),
        write_to=str(dest_png),
        output_width=RASTER_WIDTH,
        background_color="white",
    )
    # Read back the produced dimensions for the library row (best-effort).
    try:
        from PIL import Image

        with Image.open(dest_png) as im:
            return im.width, im.height
    except Exception:  # noqa: BLE001 — Pillow optional; fall back to the nominal
        return RASTER_WIDTH, int(RASTER_WIDTH * 500 / 800)


def _existing_id(conn, file_path: str):
    row = conn.execute(
        "SELECT id FROM library WHERE file_path = ?", (file_path,)
    ).fetchone()
    return row[0] if row else None


def _upsert_row(conn, *, subject, area, begrepp, heading, png_rel, svg_rel,
                width, height, svg_src_disp, force,
                source_tag=SOURCE_TAG, backend_label=BACKEND_LABEL,
                engine="skola-svg-baseline", title=None):
    """Insert (or, with ``--force``, refresh) the library row. Returns (id, action).

    Uses the deterministic ``file_path`` as the identity. A second plain run finds
    the row and skips ('exists'); ``--force`` updates the existing row in place
    ('updated') so a re-rasterise never creates a duplicate. A brand-new row is
    'inserted'. The row carries the migration-0007 provenance columns
    (source / svg_source_path / preferred=1).

    ``source_tag`` / ``backend_label`` / ``engine`` are parameterised so a second
    vector lane (the Claude-authored-SVG gap-fill) can reuse this exact upsert with
    its own provenance (``source='authored-svg'``) without forking the SQL — the
    skola-baseline lane passes the module-level defaults, so its behaviour is
    unchanged. ``title`` is the library display title (defaults to ``begrepp`` when
    not given; the authored lane passes a richer title that may carry a
    formula/qualifier while ``begrepp`` stays the canonical concept name).
    """
    from app.db import new_id, now_iso

    file_path = str(png_rel)
    svg_path = str(svg_rel)
    row_title = title if title is not None else begrepp
    workflow = json.dumps(
        {"engine": engine, "svg_source": svg_src_disp, "heading": heading,
         "begrepp": begrepp},
        ensure_ascii=False,
    )
    existing = _existing_id(conn, file_path)
    if existing and not force:
        return existing, "exists"
    if existing:
        conn.execute(
            """
            UPDATE library SET
              subject=?, area=?, image_type=?, title=?, prompt=?,
              backend=?, width=?, height=?, workflow_json=?,
              source=?, svg_source_path=?, preferred=1, deleted_at=NULL,
              missing_since=NULL
            WHERE id=?
            """,
            (subject, area, IMAGE_TYPE, row_title, heading, backend_label,
             width, height, workflow, source_tag, svg_path, existing),
        )
        return existing, "updated"
    lib_id = new_id()
    conn.execute(
        """
        INSERT INTO library
          (id, kind, subject, area, image_type, video_type, title, job_id,
           file_path, thumb_path, prompt, lora, lora_strength, seed, backend,
           width, height, workflow_json, source, svg_source_path, preferred,
           created_at)
        VALUES (?, 'bild', ?, ?, ?, NULL, ?, NULL,
                ?, NULL, ?, NULL, NULL, NULL, ?,
                ?, ?, ?, ?, ?, 1, ?)
        """,
        (lib_id, subject, area, IMAGE_TYPE, row_title,
         file_path, heading, backend_label,
         width, height, workflow, source_tag, svg_path, now_iso()),
    )
    return lib_id, "inserted"


def resolve_all(svg_root: str, fysik_json: str | None):
    """Resolve every SVG → an import plan record. Pure (no writes).

    Yields dicts: ``{svg, subject, area, begrepp, heading, path, mapped}``. An
    unmapped fysik SVG has ``mapped=False`` and subject/area/begrepp = None.
    """
    concepts = sb.load_fysik_concepts(fysik_json)
    index = sb._build_index(concepts)
    for subdir, name, path in sb.iter_svgs(svg_root):
        key = f"{subdir}/{name}"
        heading = sb.svg_heading(path)
        if subdir in sb.FYSIK_SUBDIRS:
            concept = sb.resolve_fysik(subdir, name, heading, concepts, _index=index)
            if concept:
                yield {"svg": key, "subject": "fysik", "area": concept["area"],
                       "begrepp": concept["begrepp"], "heading": heading,
                       "path": path, "name": name, "mapped": True}
            else:
                yield {"svg": key, "subject": "fysik", "area": None,
                       "begrepp": None, "heading": heading, "path": path,
                       "name": name, "mapped": False}
        elif subdir in sb.TEKNIK_SUBDIRS:
            begrepp, area = sb.TEKNIK_CONCEPTS[key]
            yield {"svg": key, "subject": "teknik", "area": area,
                   "begrepp": begrepp, "heading": heading, "path": path,
                   "name": name, "mapped": True}


def run_import(svg_root: str, fysik_json: str | None, *, dry_run: bool, force: bool):
    """Import all mapped SVGs into the live library + storage. Returns a summary."""
    from app.db import connect, init_db

    storage = Path(os.environ["LARBILD_STORAGE"]) if "LARBILD_STORAGE" in os.environ \
        else None
    db_path = os.environ.get("LARBILD_DB_PATH")
    if not dry_run:
        if storage is None or not db_path:
            raise SystemExit(
                "LARBILD_STORAGE and LARBILD_DB_PATH must be set to import "
                "(or pass --dry-run). Refusing to guess paths."
            )
        # Ensure the schema (incl. migration 0007) is present on the target DB.
        init_db(db_path)

    plans = list(resolve_all(svg_root, fysik_json))
    summary = {"inserted": 0, "updated": 0, "exists": 0, "skipped_unmapped": 0,
               "errors": 0, "by_subject": {"fysik": 0, "teknik": 0}, "items": []}

    def _record(plan, action, extra=None):
        summary["items"].append({"svg": plan["svg"], "begrepp": plan["begrepp"],
                                 "subject": plan["subject"], "area": plan["area"],
                                 "action": action, **(extra or {})})

    for plan in plans:
        if not plan["mapped"]:
            summary["skipped_unmapped"] += 1
            _record(plan, "skipped_unmapped")
            continue

        subject, area, name = plan["subject"], plan["area"], plan["name"]
        png_rel, svg_rel = _rel_paths(subject, area, name)

        if dry_run:
            summary["by_subject"][subject] += 1
            _record(plan, "would-import", {"png": str(png_rel), "svg": str(svg_rel)})
            continue

        try:
            dest_png = storage / png_rel
            dest_svg = storage / svg_rel
            dest_svg.parent.mkdir(parents=True, exist_ok=True)
            # Copy the vector verbatim (READ-only source).
            shutil.copyfile(plan["path"], dest_svg)
            width, height = _rasterise(plan["path"], dest_png)
            with connect() as conn:
                lib_id, action = _upsert_row(
                    conn, subject=subject, area=area, begrepp=plan["begrepp"],
                    heading=plan["heading"], png_rel=png_rel, svg_rel=svg_rel,
                    width=width, height=height, svg_src_disp=plan["svg"], force=force,
                )
            summary[action] += 1
            summary["by_subject"][subject] += 1
            _record(plan, action, {"id": lib_id, "png": str(png_rel)})
        except Exception as e:  # noqa: BLE001 — keep going; report at end
            summary["errors"] += 1
            _record(plan, "error", {"error": str(e)})

    return summary


# ── authored-svg gap-fill lane ──────────────────────────────────────────────


def _validate_authored_concept(begrepp, area, fysik_json):
    """Cross-check a manifest entry against the larbild fysik authority.

    Returns ``None`` if (begrepp, area) is a real fysik concept landing in its
    AUTHORITATIVE area, else a short reason string. This keeps an authored SVG from
    being filed under the wrong Bibliotek node or against a non-existent concept —
    the same single-source-of-truth (fysik.json's ``area`` wins) rule the skola
    lane enforces via :func:`app.svg_baseline.resolve_fysik`.
    """
    try:
        from app import svg_baseline as sb
        concepts = sb.load_fysik_concepts(fysik_json)
    except Exception:  # noqa: BLE001 — if the authority can't load, skip the check
        return None
    by_norm, _ = sb._build_index(concepts)
    c = by_norm.get(sb.normalize(begrepp))
    if c is None:
        return f"begrepp {begrepp!r} not in fysik concept set"
    if c.get("area") != area:
        return f"begrepp {begrepp!r} belongs to area {c.get('area')!r}, not {area!r}"
    return None


def resolve_authored(authored_root: str, manifest_path: str, fysik_json):
    """Resolve every Claude-authored fysik SVG → an import plan record. Pure.

    Reads the manifest (``<area>/<slug>`` → ``{begrepp, title}``) and pairs each
    entry with its on-disk SVG at ``<authored_root>/<area>/<slug>.svg``. The parent
    dir (the manifest key's area) is the authoritative Bibliotek node, cross-checked
    against fysik.json. Yields dicts:
    ``{svg, subject, area, begrepp, title, heading, path, name, mapped, reason?}``.
    A manifest entry whose SVG is missing, or whose (begrepp, area) fails the
    authority check, has ``mapped=False`` + a ``reason``.
    """
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    subject = manifest.get("subject", "fysik")
    root = Path(authored_root)
    for key, meta in manifest["items"].items():
        area, name = key.split("/", 1)
        begrepp = meta["begrepp"]
        title = meta.get("title", begrepp)
        svg_path = root / area / f"{name}.svg"
        if not svg_path.exists():
            yield {"svg": key, "subject": subject, "area": area, "begrepp": begrepp,
                   "title": title, "heading": None, "path": svg_path, "name": name,
                   "mapped": False, "reason": f"SVG not found at {svg_path}"}
            continue
        reason = _validate_authored_concept(begrepp, area, fysik_json)
        if reason:
            yield {"svg": key, "subject": subject, "area": area, "begrepp": begrepp,
                   "title": title, "heading": None, "path": svg_path, "name": name,
                   "mapped": False, "reason": reason}
            continue
        from app import svg_baseline as sb
        yield {"svg": key, "subject": subject, "area": area, "begrepp": begrepp,
               "title": title, "heading": sb.svg_heading(svg_path), "path": svg_path,
               "name": name, "mapped": True}


def run_authored_import(authored_root: str, manifest_path: str, fysik_json,
                        *, dry_run: bool, force: bool):
    """Import the Claude-authored gap-fill SVGs into the live library + storage.

    Mirrors :func:`run_import` but for the authored lane: same verbatim-SVG copy +
    sanitised rasterise + ``_upsert_row``, but with ``source='authored-svg'`` and
    the ``__authored-svg`` suffix, so authored baselines coexist with both the
    skola baselines and the batch renders, and surface preferred-first. Idempotent
    by the deterministic ``file_path``; ``--force`` re-rasterises in place.
    """
    from app.db import connect, init_db

    storage = Path(os.environ["LARBILD_STORAGE"]) if "LARBILD_STORAGE" in os.environ \
        else None
    db_path = os.environ.get("LARBILD_DB_PATH")
    if not dry_run:
        if storage is None or not db_path:
            raise SystemExit(
                "LARBILD_STORAGE and LARBILD_DB_PATH must be set to import "
                "(or pass --dry-run). Refusing to guess paths."
            )
        init_db(db_path)

    plans = list(resolve_authored(authored_root, manifest_path, fysik_json))
    summary = {"inserted": 0, "updated": 0, "exists": 0, "skipped_unmapped": 0,
               "errors": 0, "by_subject": {"fysik": 0}, "items": []}

    def _record(plan, action, extra=None):
        summary["items"].append({"svg": plan["svg"], "begrepp": plan["begrepp"],
                                 "subject": plan["subject"], "area": plan["area"],
                                 "action": action, **(extra or {})})

    for plan in plans:
        if not plan["mapped"]:
            summary["skipped_unmapped"] += 1
            _record(plan, "skipped_unmapped", {"reason": plan.get("reason")})
            continue

        subject, area, name = plan["subject"], plan["area"], plan["name"]
        png_rel, svg_rel = _rel_paths(subject, area, name, AUTHORED_SUFFIX)

        if dry_run:
            summary["by_subject"].setdefault(subject, 0)
            summary["by_subject"][subject] += 1
            _record(plan, "would-import", {"png": str(png_rel), "svg": str(svg_rel)})
            continue

        try:
            dest_png = storage / png_rel
            dest_svg = storage / svg_rel
            dest_svg.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(plan["path"], dest_svg)
            width, height = _rasterise(plan["path"], dest_png)
            with connect() as conn:
                lib_id, action = _upsert_row(
                    conn, subject=subject, area=area, begrepp=plan["begrepp"],
                    heading=plan["heading"], png_rel=png_rel, svg_rel=svg_rel,
                    width=width, height=height, svg_src_disp=plan["svg"], force=force,
                    source_tag=AUTHORED_SOURCE_TAG, backend_label=AUTHORED_BACKEND_LABEL,
                    engine="authored-svg", title=plan["title"],
                )
            summary[action] += 1
            summary["by_subject"].setdefault(subject, 0)
            summary["by_subject"][subject] += 1
            _record(plan, action, {"id": lib_id, "png": str(png_rel)})
        except Exception as e:  # noqa: BLE001 — keep going; report at end
            summary["errors"] += 1
            _record(plan, "error", {"error": str(e)})

    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--svg-root", default=DEFAULT_SVG_ROOT)
    ap.add_argument("--fysik-json", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="re-rasterise + refresh existing rows (still no dupes)")
    ap.add_argument("--authored", action="store_true",
                    help="import the Claude-authored gap-fill SVG lane "
                         "(source='authored-svg') instead of the skola baseline")
    ap.add_argument("--authored-root", default=DEFAULT_AUTHORED_ROOT,
                    help="root of the Claude-authored SVGs (with --authored)")
    ap.add_argument("--authored-manifest", default=DEFAULT_AUTHORED_MANIFEST,
                    help="manifest mapping <area>/<slug> -> begrepp/title (with --authored)")
    args = ap.parse_args()

    if args.authored:
        summary = run_authored_import(
            args.authored_root, args.authored_manifest, args.fysik_json,
            dry_run=args.dry_run, force=args.force,
        )
        lane = "AUTHORED-SVG"
    else:
        summary = run_import(args.svg_root, args.fysik_json,
                             dry_run=args.dry_run, force=args.force)
        lane = "SKOLA-SVG"
    mode = "DRY-RUN" if args.dry_run else "IMPORT"
    print(f"[{mode} {lane}] inserted={summary['inserted']} updated={summary['updated']} "
          f"exists={summary['exists']} skipped_unmapped={summary['skipped_unmapped']} "
          f"errors={summary['errors']}")
    print("  by_subject: " + " ".join(
        f"{k}={v}" for k, v in sorted(summary["by_subject"].items())))
    # Surface any skipped (unmapped/missing) entries with their reason — for the
    # authored lane this is the signal an SVG/manifest pairing is off.
    for it in summary["items"]:
        if it["action"] == "skipped_unmapped" and it.get("reason"):
            print(f"  SKIP {it['svg']}: {it['reason']}")
    if summary["errors"]:
        for it in summary["items"]:
            if it["action"] == "error":
                print(f"  ERROR {it['svg']}: {it.get('error')}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
