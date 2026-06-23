"""lb-1gi — reconcile the matte-svg-gen rows onto the matematik taxonomy areas.

The 104 ``source = 'matte-svg-gen'`` library rows were imported under granular,
year-prefixed concept slugs (``ak7-vinklar``, ``ak8-volym``, …) in their
``area`` column. Every *other* asset in the library — fysik, teknik, and the
older skola-diagram matematik rows — stores the COARSE LGR22 taxonomy area slug
(``geometri``, ``el-magnet``, …) in ``area``; that is the single mechanism the
Bibliotek nav joins on (``app/routes/library.py`` left-joins ``(subject, area)``
counts onto the taxonomy tree, and ``get_library_items`` validates ``area``
against the taxonomy SoT). Because ``ak7-*`` is not a taxonomy area, these rows
were review-approved and served via ``/media`` but never appeared in the
matematik nav tree.

This migration corrects the DATA to match how every other row is stored: it
remaps each row's ``area`` from its granular concept slug to the one coarse
matematik taxonomy area (one of the six the SoT defines) that the concept
belongs to. The concept name is preserved verbatim in ``title`` (it always
was), and ``file_path`` is left untouched — it is the opaque storage key the
unique index + the ``/media`` route serve by, decoupled from the nav ``area``.
No new runtime mapping layer is added (no fork): the existing area-slug
mechanism is simply applied to data that bypassed it at import time.

Idempotent: it only rewrites rows whose ``area`` is still a granular ``ak*``
concept slug present in :data:`AREA_MAP`. A re-run finds none (they now carry
coarse slugs) and is a no-op, satisfying the runner's attempt-twice contract.
Target area slugs are asserted against the taxonomy SoT at apply time, so a
future taxonomy edit that drops an area fails loud here rather than silently
re-hiding rows.

SFW-only: a pure data reconcile over the SFW library DB; no restricted column,
content, or reference.
"""
from __future__ import annotations

import sqlite3

# Absolute import: the runner loads .py migrations via spec_from_file_location
# under a synthetic module name (``_lb_migration_…``), so this module has no
# parent package — a relative ``from .. import taxonomy`` raises ImportError.
# ``app.taxonomy`` is always importable when migrations run (the app package is
# on the path).
from app import taxonomy

_SUBJECT = "matematik"

# Granular concept slug -> coarse matematik taxonomy area slug. Each ak*-concept
# is placed in the one area whose Skolverket central content covers it:
#   taluppfattning  Bråk, decimaltal, procent, potenser, grundpotensform
#   algebra         Uttryck, ekvationer, olikheter, funktioner, mönster
#   geometri        Vinklar, area, volym, Pythagoras, skala, likformighet
#   sannolikhet     Sannolikhet, kombinatorik, lägesmått, diagram, spridning
#   samband         Proportionalitet, linjära funktioner, procentuell förändring
#   problemlosning  Vardagsekonomi, optimering, flerstegsproblem
AREA_MAP: dict[str, str] = {
    # --- åk 7 ---
    "ak7-algebraiska-uttryck-intro": "algebra",
    "ak7-brak": "taluppfattning",
    "ak7-cirkel-grund": "geometri",
    "ak7-decimaltal": "taluppfattning",
    "ak7-direkt-proportionalitet": "samband",
    "ak7-enkla-ekvationer": "algebra",
    "ak7-forhallande-och-proportion": "samband",
    "ak7-fyrhorningar": "geometri",
    "ak7-hastighet-stracka-tid-grund": "samband",
    "ak7-koordinatsystem-grund": "algebra",
    "ak7-monster-och-formler": "algebra",
    "ak7-negativa-tal": "taluppfattning",
    "ak7-omkrets-och-area": "geometri",
    "ak7-procent": "taluppfattning",
    "ak7-procentuell-forandring-grund": "samband",
    "ak7-raknesatt-och-prioritet": "taluppfattning",
    "ak7-tabeller-och-diagram": "sannolikhet",
    "ak7-trianglar": "geometri",
    "ak7-vinklar": "geometri",
    # --- åk 8 ---
    "ak8-algebra-multiplikation-parentes": "algebra",
    "ak8-algebra-uttryck": "algebra",
    "ak8-brak-addition-subtraktion": "taluppfattning",
    "ak8-brak-forlangning-forkortning": "taluppfattning",
    "ak8-brak-grundbegrepp": "taluppfattning",
    "ak8-cirkel": "geometri",
    "ak8-ekvationer": "algebra",
    "ak8-geometri-figurer": "geometri",
    "ak8-geometri-omkrets-area": "geometri",
    "ak8-kombinatorik": "sannolikhet",
    "ak8-potenser": "taluppfattning",
    "ak8-potenser-grundpotensform": "taluppfattning",
    "ak8-procent-andel": "taluppfattning",
    "ak8-procent-brak-decimal": "taluppfattning",
    "ak8-sannolikhet": "sannolikhet",
    "ak8-skala": "geometri",
    "ak8-statistik-lagesmatt": "sannolikhet",
    "ak8-tal-decimaltal": "taluppfattning",
    "ak8-volym": "geometri",
    # --- åk 9 ---
    "ak9-algebraiska-uttryck": "algebra",
    "ak9-blandning-och-koncentration": "problemlosning",
    "ak9-diagram": "sannolikhet",
    "ak9-ekvationssystem-och-flerstegs": "problemlosning",
    "ak9-frekvenstabell": "sannolikhet",
    "ak9-hastighet-stracka-tid": "samband",
    "ak9-komplementhandelse": "sannolikhet",
    "ak9-kvadratrotter": "taluppfattning",
    "ak9-linjar-funktion-graf": "algebra",
    "ak9-linjar-funktion-k-och-m": "algebra",
    "ak9-linjara-funktioner": "algebra",
    "ak9-omvand-proportionalitet": "samband",
    "ak9-optimering": "problemlosning",
    "ak9-primtal-och-delbarhet": "taluppfattning",
    "ak9-pythagoras-2d": "geometri",
    "ak9-sammansatt-ranta": "problemlosning",
    "ak9-sammansatta-figurer": "geometri",
    "ak9-skala-och-likformighet": "geometri",
    "ak9-statistik-i-tillampning": "sannolikhet",
    "ak9-stickprov": "sannolikhet",
}


def _validate_targets() -> None:
    """Fail loud if any mapping target is not a real matematik taxonomy area.

    Guards against silently re-hiding rows under a non-existent area if the
    taxonomy SoT ever drops/renames one of the six areas.
    """
    valid = set(taxonomy.area_slugs(_SUBJECT))
    bad = sorted(set(AREA_MAP.values()) - valid)
    if bad:
        raise ValueError(
            f"0010_matte_area_reconcile: target area(s) not in the {_SUBJECT} "
            f"taxonomy SoT: {bad} (valid: {sorted(valid)})"
        )


def up(conn: sqlite3.Connection) -> None:
    """Remap matte-svg-gen rows' granular ``area`` to the coarse taxonomy area.

    Only rewrites a row when its current ``area`` is still one of the granular
    concept slugs in :data:`AREA_MAP` — so a re-run (every worker boot) touches
    nothing. Scoped to ``subject = 'matematik'`` AND ``source = 'matte-svg-gen'``
    so it can never touch fysik/teknik or the older skola-diagram matematik rows.
    """
    _validate_targets()
    for old_slug, new_slug in AREA_MAP.items():
        conn.execute(
            "UPDATE library SET area = ? "
            "WHERE subject = ? AND source = 'matte-svg-gen' AND area = ?",
            (new_slug, _SUBJECT, old_slug),
        )
