#!/usr/bin/env python3
"""Import skola's matematik + teknik *berakning* diagram images into the Lärbild review library.

The skola question bank carries 387 matematik + 15 teknik calculation questions
whose figure is a code/matplotlib-RENDERED PNG (``question_bank.image_url`` →
``/concept-images/matte-diagram/<question_id>.png``). The owner wants to review
each one in the Lärbild Bibliotek the same way the fysik concept diagrams are
reviewable — approve / Inte-OK with a note. The 84 that failed skola's generation
gate (``gate_status`` ∈ {vision_gate_failed, code_validation_failed}) are exactly
the ones that most need eyes, so they are imported too, with the gate verdict
surfaced.

This is the THIRD library import lane, a sibling of ``import_skola_svgs.py``'s two
SVG-vector lanes (skola-svg-baseline + authored-svg). The mechanism is identical —
copy the artefact into Lärbild STORAGE_DIR and INSERT a ``library`` row so the
existing Bibliotek + review UI surface it with no UI fork — but the source artefact
is a finished PNG (no rasterisation; we just copy + read its dimensions) and a
THIRD provenance tag, ``source='skola-diagram'``, distinguishes it.

WHY copy (not reference): Lärbild runs on Sibbis and serves images from
``LARBILD_STORAGE`` (an NFS mount from vm153, ``/mnt/vm153-output/larbild``); the
``/media`` route only serves files UNDER that root. The skola PNGs live on VM500,
unreachable from Sibbis, so referencing them by URL is impossible — the fysik lane
copies for the same reason. The PNGs are tiny (≈7.4 MB total), so a verbatim copy
is cheap and matches the established pattern exactly.

Differences from the SVG lanes, all deliberate:
  * ``preferred`` is left NULL — a skola berakning diagram is NOT a hand-authored
    vector baseline; it is review fodder, not a "show this first" canonical asset.
    (The SVG lanes set preferred=1; this lane does not.)
  * ``review_ok`` is left NULL = pending, so EVERY imported diagram lands in the
    "Att granska" filter for the owner to verdict. A failed gate does NOT pre-set
    Inte-OK — the owner makes the call; the gate verdict is shown as context.
  * The gate verdict + skola question id + question prompt are recorded in
    ``prompt`` (human-visible in the lightbox) and ``workflow_json`` (structured,
    incl. ``skola_question_id`` for a backlink) so the owner sees WHY a row is
    flagged and can trace it to the skola question.

Subject/area mapping (skola strand → Lärbild taxonomy area slug, lb-582 authority):
    matematik: geometri→geometri · samband-forandring→samband ·
               sannolikhet-statistik→sannolikhet · taluppfattning→taluppfattning
    teknik:    mekanismer→mekanismer · komponenter-elektronik→elektronik_styr ·
               styrning-reglering→elektronik_styr
Every (subject, area) pair is validated against the Lärbild taxonomy before a row
is written; an unmapped strand is reported and skipped, never filed under a guess.

INPUT — a manifest + a directory of PNGs, both produced by
``extract_skola_diagrams.sh`` on VM500 (which has the skola DB + the PNGs):
  * ``--manifest`` — a JSON list of records:
        {"question_id", "subject", "strand", "area_slug" (skola's), "image_file"
         (basename), "gate_status", "level", "prompt"}
  * ``--png-root`` — the directory holding the ``image_file`` PNGs.

IDEMPOTENT + NON-DESTRUCTIVE + batch-safe (same contract as the SVG lanes):
  * The deterministic ``file_path`` (carrying a distinct ``__skola-diagram``
    suffix) is the identity — a plain re-run skips already-imported rows
    ('exists'); ``--force`` re-copies + refreshes them in place ('updated'),
    never duplicating. The suffix means a row can never collide with a batch
    render or either SVG lane for the same node.
  * Source PNGs are READ-only (copied, never moved).

Reads ``LARBILD_DB_PATH`` + ``LARBILD_STORAGE`` from the env (the vars the service
uses), so on the deploy host it writes straight into the live library + storage.
``--dry-run`` resolves + reports without writing.

Usage (on Sibbis, after extract_skola_diagrams.sh has staged the manifest+PNGs):
    LARBILD_DB_PATH=/home/lektove/larbild-app/data/larbild.db \
    LARBILD_STORAGE=/mnt/vm153-output/larbild \
      python3 scripts/import_skola_diagrams.py \
        --manifest /tmp/skola-diagrams/manifest.json \
        --png-root /tmp/skola-diagrams/png \
        [--dry-run] [--force]

SFW-only: educational calculation diagrams into the SFW library; no restricted content.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

# Run from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import taxonomy  # noqa: E402

# Provenance tag written to library.source — a THIRD lane alongside
# 'skola-svg-baseline' and 'authored-svg'. The review loop / gallery read it to
# know a row is a skola-rendered calculation diagram (review fodder, not a vector
# baseline).
SOURCE_TAG = "skola-diagram"
# Backend label — honest about HOW the image was produced (skola rendered the
# figure from code), mirroring the "skola-svg · vector" convention.
BACKEND_LABEL = "skola-diagram · render"
# The library bucket — these are all diagrams.
IMAGE_TYPE = "diagram"
# Distinct filename suffix → its own deterministic file_path, so a skola-diagram
# import never clashes with a batch render or either SVG lane for the same node.
SUFFIX = "__skola-diagram"

# skola question-bank strand → Lärbild taxonomy area slug. The strand is skola's
# coarse curriculum band (stable across its åk7/8/9 area slugs); it maps 1:1 onto
# a Lärbild matematik/teknik area. Every value here is validated against the
# taxonomy at import time — a strand missing from this map is reported + skipped.
STRAND_TO_AREA = {
    "matematik": {
        "geometri": "geometri",
        "samband-forandring": "samband",
        "sannolikhet-statistik": "sannolikhet",
        "taluppfattning": "taluppfattning",
        # algebra / problemlosning exist in the Lärbild taxonomy but the skola
        # corpus has no diagram images for them yet — added here when it does.
        "algebra": "algebra",
        "problemlosning": "problemlosning",
    },
    "teknik": {
        "mekanismer": "mekanismer",
        "komponenter-elektronik": "elektronik_styr",
        "styrning-reglering": "elektronik_styr",
    },
}

# A short, human-readable label for a skola gate verdict — shown in the lightbox
# prompt so the owner sees at a glance WHY a flagged row needs attention. Unknown
# / null verdicts pass through verbatim.
_GATE_LABEL = {
    "passed": "GATE: godkänd",
    "manual_review": "GATE: manuell granskning",
    "vision_gate_failed": "GATE UNDERKÄND (vision)",
    "code_validation_failed": "GATE UNDERKÄND (kodvalidering)",
}


def _resolve_area(subject: str, strand: str) -> str | None:
    """Map (subject, strand) → a VALID Lärbild area slug, or None if unmappable.

    None means either the subject isn't in the strand map or the strand isn't,
    OR the mapped slug fails the taxonomy authority check — in every case the
    caller skips the row and reports it, never guessing an area.
    """
    area = STRAND_TO_AREA.get(subject, {}).get(strand)
    if area is None:
        return None
    if not taxonomy.is_valid_area(subject, area):
        return None
    return area


def _rel_path(subject: str, area: str, question_id: str) -> Path:
    """STORAGE_DIR-relative PNG path for one imported diagram.

    Lives under ``output/<subject>/<area>/diagram/`` (so it sorts into the right
    Bibliotek node) with the ``__skola-diagram`` suffix so it never clashes with a
    batch render or an SVG-lane asset for the same concept.
    """
    base = Path("output") / subject / area / IMAGE_TYPE
    return base / f"{question_id}{SUFFIX}.png"


def _png_dims(png_path: Path) -> tuple[int | None, int | None]:
    """(width, height) of a PNG, best-effort. None,None if Pillow is absent or the
    file can't be read — dimensions are display metadata, not load-bearing."""
    try:
        from PIL import Image

        with Image.open(png_path) as im:
            return im.width, im.height
    except Exception:  # noqa: BLE001 — Pillow optional / unreadable file
        return None, None


def _display_prompt(rec: dict, area: str) -> str:
    """The lightbox ``prompt`` text: gate verdict + the skola question stem.

    Leads with the gate label (so a flagged row reads "why" first), then the
    question prompt. This is what the owner sees in the review lightbox alongside
    the image — enough to verdict without opening skola.
    """
    gate = rec.get("gate_status")
    gate_lbl = _GATE_LABEL.get(gate, f"GATE: {gate}" if gate else "GATE: (okänd)")
    stem = (rec.get("prompt") or "").strip()
    level = rec.get("level")
    bits = [gate_lbl]
    if level:
        bits.append(f"nivå {level}")
    head = " · ".join(bits)
    return f"{head}\n{stem}" if stem else head


def _existing_id(conn, file_path: str):
    row = conn.execute(
        "SELECT id FROM library WHERE file_path = ?", (file_path,)
    ).fetchone()
    return row[0] if row else None


def _upsert_row(conn, *, subject, area, question_id, title, prompt, png_rel,
                width, height, workflow_json, force):
    """Insert (or, with ``--force``, refresh) the library row. Returns (id, action).

    Identity is the deterministic ``file_path``: a plain re-run finds the row and
    skips ('exists'); ``--force`` updates it in place ('updated'); a brand-new row
    is 'inserted'. ``review_ok`` is left NULL (pending) and ``preferred`` NULL —
    a skola render is review fodder, not a canonical vector baseline.
    """
    from app.db import new_id, now_iso

    file_path = str(png_rel)
    existing = _existing_id(conn, file_path)
    if existing and not force:
        return existing, "exists"
    if existing:
        conn.execute(
            """
            UPDATE library SET
              subject=?, area=?, image_type=?, title=?, prompt=?,
              backend=?, width=?, height=?, workflow_json=?,
              source=?, preferred=NULL, deleted_at=NULL, missing_since=NULL
            WHERE id=?
            """,
            (subject, area, IMAGE_TYPE, title, prompt, BACKEND_LABEL,
             width, height, workflow_json, SOURCE_TAG, existing),
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
                ?, ?, ?, ?, NULL, NULL, ?)
        """,
        (lib_id, subject, area, IMAGE_TYPE, title,
         file_path, prompt, BACKEND_LABEL,
         width, height, workflow_json, SOURCE_TAG, now_iso()),
    )
    return lib_id, "inserted"


def _title(rec: dict, area: str) -> str:
    """A short library display title — the question's skola area name (its band)
    plus a truncated stem so a node of similar questions is still distinguishable.
    """
    name = (rec.get("area_name") or rec.get("area_slug") or area).strip()
    stem = (rec.get("prompt") or "").strip()
    if stem:
        short = stem[:60].rstrip()
        if len(stem) > 60:
            short += "…"
        return f"{name}: {short}"
    return name


def run_import(manifest_path: str, png_root: str, *, dry_run: bool, force: bool):
    """Import every mapped diagram into the live library + storage. Returns a summary."""
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
        # Ensure the schema (incl. the review + provenance migrations) is present.
        init_db(db_path)

    records = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    png_dir = Path(png_root)

    summary = {
        "inserted": 0, "updated": 0, "exists": 0,
        "skipped_unmapped": 0, "skipped_missing_png": 0, "errors": 0,
        "by_subject": {}, "by_gate": {}, "items": [],
    }

    def _record(rec, action, extra=None):
        summary["items"].append(
            {"question_id": rec.get("question_id"), "subject": rec.get("subject"),
             "strand": rec.get("strand"), "gate_status": rec.get("gate_status"),
             "action": action, **(extra or {})}
        )

    for rec in records:
        subject = (rec.get("subject") or "").strip()
        strand = (rec.get("strand") or "").strip()
        qid = (rec.get("question_id") or "").strip()
        image_file = (rec.get("image_file") or "").strip()
        gate = rec.get("gate_status") or "(null)"

        area = _resolve_area(subject, strand)
        if area is None:
            summary["skipped_unmapped"] += 1
            _record(rec, "skipped_unmapped",
                    {"reason": f"no taxonomy area for {subject}/{strand}"})
            continue

        png_rel = _rel_path(subject, area, qid)
        src_png = png_dir / image_file

        if not dry_run and not src_png.is_file():
            summary["skipped_missing_png"] += 1
            _record(rec, "skipped_missing_png", {"reason": f"PNG not found: {src_png}"})
            continue

        if dry_run:
            summary["by_subject"][subject] = summary["by_subject"].get(subject, 0) + 1
            summary["by_gate"][gate] = summary["by_gate"].get(gate, 0) + 1
            _record(rec, "would-import", {"area": area, "png": str(png_rel)})
            continue

        try:
            dest_png = storage / png_rel
            dest_png.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_png, dest_png)  # READ-only source
            width, height = _png_dims(dest_png)
            prompt = _display_prompt(rec, area)
            title = _title(rec, area)
            workflow = json.dumps(
                {"engine": "skola-diagram",
                 "skola_question_id": qid,
                 "skola_area_slug": rec.get("area_slug"),
                 "skola_strand": strand,
                 "gate_status": rec.get("gate_status"),
                 "level": rec.get("level"),
                 "image_url": rec.get("image_url")},
                ensure_ascii=False,
            )
            with connect() as conn:
                lib_id, action = _upsert_row(
                    conn, subject=subject, area=area, question_id=qid,
                    title=title, prompt=prompt, png_rel=png_rel,
                    width=width, height=height, workflow_json=workflow, force=force,
                )
            summary[action] += 1
            summary["by_subject"][subject] = summary["by_subject"].get(subject, 0) + 1
            summary["by_gate"][gate] = summary["by_gate"].get(gate, 0) + 1
            _record(rec, action, {"id": lib_id, "area": area, "png": str(png_rel)})
        except Exception as e:  # noqa: BLE001 — keep going; report at end
            summary["errors"] += 1
            _record(rec, "error", {"error": str(e)})

    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True,
                    help="JSON manifest from extract_skola_diagrams.sh")
    ap.add_argument("--png-root", required=True,
                    help="directory holding the manifest's image_file PNGs")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve + report without copying or writing")
    ap.add_argument("--force", action="store_true",
                    help="re-copy + refresh existing rows (still no dupes)")
    args = ap.parse_args()

    summary = run_import(args.manifest, args.png_root,
                         dry_run=args.dry_run, force=args.force)
    mode = "DRY-RUN" if args.dry_run else "IMPORT"
    print(f"[{mode} SKOLA-DIAGRAM] inserted={summary['inserted']} "
          f"updated={summary['updated']} exists={summary['exists']} "
          f"skipped_unmapped={summary['skipped_unmapped']} "
          f"skipped_missing_png={summary['skipped_missing_png']} "
          f"errors={summary['errors']}")
    print("  by_subject: " + " ".join(
        f"{k}={v}" for k, v in sorted(summary["by_subject"].items())))
    print("  by_gate: " + " ".join(
        f"{k}={v}" for k, v in sorted(summary["by_gate"].items())))
    for it in summary["items"]:
        if it["action"] in ("skipped_unmapped", "skipped_missing_png") and it.get("reason"):
            print(f"  SKIP {it.get('question_id')}: {it['reason']}")
    if summary["errors"]:
        for it in summary["items"]:
            if it["action"] == "error":
                print(f"  ERROR {it.get('question_id')}: {it.get('error')}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
