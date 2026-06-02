"""Map the 123 Claude-authored fysik/teknik concept SVGs → Lärbild concepts.

The skola rig shipped 123 exact, perfectly-Swedish-labelled vector concept
diagrams (``frontend/public/concept-images/<subdir>/<name>.svg``, viewBox
``0 0 800 500``, white background, real ``<text>`` elements). They are strictly
better than diffusion/matplotlib for these structured concepts, so they become
the DIAGRAM baseline (the *preferred* version) for the concepts they cover.

This module is the pure, deterministic, side-effect-free mapping layer that the
import script (``scripts/import_skola_svgs.py``) and the test
(``tests/test_svg_baseline.py``) both build on:

* :func:`resolve_fysik` — match a fysik SVG (its filename slug + its heading
  ``<text>``) to a Lärbild *fysik* begrepp, using the larbild fysik concept set
  (``/tmp/larbild-begrepp/fysik.json`` or a passed-in list) as the authority for
  the concept name **and its area** (the concept's ``area`` field is the source of
  truth for which Bibliotek node the baseline lands in — NOT the SVG subdir, which
  is only a hint). Accent/case-insensitive, with a conservative Swedish
  definite/plural stemmer and a small hand-verified alias table for the cases the
  fuzzy matcher can't reach. Returns the matched begrepp + area, or ``None`` when
  there is no clear concept (the GAP — a candidate for the SVG-authoring lane).

* :data:`TEKNIK_CONCEPTS` — the larbild *teknik* subject has no shipped begrepp
  dataset, so the 25 teknik SVGs each DEFINE a concept row: a hand-assigned
  ``(begrepp, area)`` derived from the SVG heading, mapped to one of the six
  larbild teknik areas (taxonomy SoT). These are *created* concepts (only for the
  concepts an SVG actually covers), per the task.

* :func:`svg_heading` / :func:`normalize` — the shared text helpers.

Matching is intentionally CONSERVATIVE: a wrong map (an SVG attached to the wrong
concept) is worse than an honest "unmapped", because unmapped SVGs are exactly the
signal the next phase (Claude authors new SVGs for the gaps) consumes. Every match
is therefore exact / stemmed / a verified alias / a same-area full-token-subset;
short-string sequence-ratio coincidences (``newton`` ≈ ``neutron``) are rejected.

SFW-only: this is curriculum concept-diagram routing; no restricted content.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Iterable, Optional

__all__ = [
    "FYSIK_SUBDIRS",
    "TEKNIK_SUBDIRS",
    "SUBDIR_AREA",
    "FYSIK_ALIAS",
    "TEKNIK_CONCEPTS",
    "normalize",
    "stem_phrase",
    "svg_heading",
    "svg_title",
    "load_fysik_concepts",
    "resolve_fysik",
    "iter_svgs",
]

# ── SVG taxonomy: which subdirs are fysik vs teknik ─────────────────────────
FYSIK_SUBDIRS = frozenset({
    "akustik", "astronomi", "atomfysik", "elektricitet", "energikallor",
    "grundbegrepp", "mekanik", "meteorologi", "optik",
})
TEKNIK_SUBDIRS = frozenset({
    "teknik_arbetssatt", "teknik_losningar", "teknik_samhalle",
})

# An SVG subdir → the larbild fysik area(s) its concepts should land in. Used to
# SCOPE the token-anchored fallback so a generic shared word can't pull an SVG
# into a foreign area (e.g. "ljudets hastighet" must not match grundfysik's bare
# "Hastighet"). The concept's own ``area`` field always wins for the final node;
# this only constrains the fuzzy fallback's candidate pool.
SUBDIR_AREA: dict[str, frozenset[str]] = {
    "akustik": frozenset({"ljud"}),
    "astronomi": frozenset({"universum"}),
    "atomfysik": frozenset({"atom", "ljus"}),       # some radiation concepts sit in ljus
    "elektricitet": frozenset({"el-magnet", "varme-klimat"}),
    "energikallor": frozenset({"varme-klimat", "el-magnet"}),
    "grundbegrepp": frozenset({"grundfysik"}),
    "mekanik": frozenset({"rorelse", "grundfysik", "universum", "el-magnet"}),
    "meteorologi": frozenset({"varme-klimat"}),
    "optik": frozenset({"ljus"}),
}

# Hand-verified aliases for fysik SVGs the fuzzy matcher can't reach (a non-obvious
# Swedish form, a heading that names the concept differently, or a deliberate
# "this SVG has no single clean larbild concept → unmap"). Every RHS begrepp here
# was checked to EXIST in the larbild fysik set before being added; ``None`` forces
# an honest unmap (the SVG depicts a whole-system view larbild has no single
# concept for). Key is ``"<subdir>/<name>"`` (the SVG's stable id).
FYSIK_ALIAS: dict[str, Optional[str]] = {
    # heading names the concept with a qualifier the set stores plainly:
    "mekanik/arbete": "Fysikaliskt arbete",
    "elektricitet/elektrisk_effekt": "Elektrisk effekt",
    "elektricitet/energi_oversikt": "Energiform",
    "energikallor/energikallor_oversikt": "Energikälla",
    "energikallor/geotermisk_energi": "Energikälla",
    "energikallor/karnkraftverk": "Kärnenergi",
    "atomfysik/radioaktivitet": "Radioaktiv",        # set stores the adjective form
    "optik/konvex_lins": "Samlingslins",
    "optik/konkav_lins": "Spridningslins",
    "optik/prisma_dispersion": "Prisma",
    "optik/ljusbrytning_prisma": "Prisma",
    "optik/reflektion": "Reflektera",                # set stores the verb form
    "akustik/decibelskalan": "Decibel",
    "energikallor/elnat_distribution": "Distributionsnät",
    "elektricitet/elmotorn": "Elmotor",              # definite form → indefinite concept
    "elektricitet/generatorn": "Generator",
    "grundbegrepp/dimensioner": "Dimension",
    "grundbegrepp/atomen_grundmodell": "Atom",       # the grundfysik atom diagram
    # whole-system / multi-concept SVGs with no single larbild concept → unmap.
    # These are GAP signal: each is a candidate for a NEW larbild concept later.
    "optik/absorption": None,
    "optik/vad_ar_ljus": None,
    "optik/ogat": None,                              # full eye anatomy (no single "Öga")
    "atomfysik/bohr_atommodell": None,               # full atom model (no "Atommodell")
    "atomfysik/genomtrangning": None,                # α/β/γ penetration comparison
    "atomfysik/ljusemission": None,
    "atomfysik/protoner_neutroner_elektroner": None,  # composite (3 particles in one)
    "akustik/ljud_hastighet": None,                  # speed-of-sound (no "Ljudhastighet")
    "akustik/orat_horseln": None,                    # full ear anatomy
    "akustik/staende_vagor": None,                   # standing waves (no "Stående våg")
    "astronomi/solsystemet": None,
    "astronomi/gravitationens_roll": None,
    "astronomi/stjarnors_livscykel": None,
    "energikallor/solkraft": None,                   # plant type (larbild groups as Energikälla)
    "energikallor/vattenkraft": None,
    "elektricitet/elsakerhet": None,
    "grundbegrepp/fasoverganger": None,              # no "Fasövergång" concept
    "grundbegrepp/temperaturskalor": None,           # only "Temperatur" exists
    "mekanik/kraftpilar": None,                      # free-body force arrows (method, not concept)
    "mekanik/newton_1": None,                        # Newton's laws (no concept rows)
    "mekanik/newton_2": None,
    "mekanik/newton_3": None,
    "meteorologi/stralningsbalans": None,
    "meteorologi/varmespridning": None,
    "meteorologi/vaxthuseffekten": "Växthuseffekt",  # exists (varme-klimat)
}

# The 25 teknik SVGs each DEFINE a teknik concept (no shipped teknik begrepp set).
# ``"<subdir>/<name>"`` → (begrepp, larbild teknik area slug). begrepp is the SVG
# heading with any parenthetical trailer trimmed (the canonical concept name); the
# area is one of the six larbild teknik areas (taxonomy SoT). These rows are
# CREATED by the import (only for concepts an SVG covers).
TEKNIK_CONCEPTS: dict[str, tuple[str, str]] = {
    "teknik_arbetssatt/algoritm": ("Algoritm", "elektronik_styr"),
    "teknik_arbetssatt/programmering": ("Programmering", "elektronik_styr"),
    "teknik_arbetssatt/variabel_loop": ("Variabel och loop", "elektronik_styr"),
    "teknik_arbetssatt/skiss_ritning": ("Skiss och ritning", "konstruktion"),
    "teknik_arbetssatt/teknikutvecklingsprocessen": ("Teknikutvecklingsprocessen", "konstruktion"),
    "teknik_losningar/bojning": ("Böjning", "material"),
    "teknik_losningar/draghallfasthet": ("Draghållfasthet", "material"),
    "teknik_losningar/hallfasthet": ("Hållfasthet", "material"),
    "teknik_losningar/hardhet_elasticitet": ("Hårdhet och elasticitet", "material"),
    "teknik_losningar/tryckhallfasthet": ("Tryckhållfasthet", "material"),
    "teknik_losningar/vridning_torsion": ("Vridning", "material"),
    "teknik_losningar/elektronik_komponenter": ("Elektronik", "elektronik_styr"),
    "teknik_losningar/sensor_styrning": ("Sensorer och styrning", "elektronik_styr"),
    "teknik_losningar/elnat_uppbyggnad": ("Elnät", "tekniska_system"),
    "teknik_losningar/internet": ("Internet", "tekniska_system"),
    "teknik_losningar/kommunikationssystem": ("Kommunikationssystem", "tekniska_system"),
    "teknik_losningar/vatten_avlopp": ("Vatten- och avloppssystem", "tekniska_system"),
    "teknik_losningar/industriell_process": ("Industriell process", "tekniska_system"),
    "teknik_samhalle/datalagring_integritet": ("Datalagring och integritet", "teknik_samhalle"),
    "teknik_samhalle/drivkrafter": ("Teknikutvecklingens drivkrafter", "teknik_samhalle"),
    "teknik_samhalle/hallbar_utveckling": ("Hållbar utveckling", "teknik_samhalle"),
    "teknik_samhalle/livscykelanalys": ("Livscykelanalys", "teknik_samhalle"),
    "teknik_samhalle/materialval_miljo": ("Materialval och miljöpåverkan", "teknik_samhalle"),
    "teknik_samhalle/sakerhet": ("Säkerhet vid teknikanvändning", "teknik_samhalle"),
    "teknik_samhalle/vetenskap_teknik": ("Vetenskap och teknik", "teknik_samhalle"),
}

# Default location of the larbild fysik concept dump (the matching authority).
DEFAULT_FYSIK_JSON = "/tmp/larbild-begrepp/fysik.json"

# Swedish stopwords dropped from token-overlap so a shared "och"/"av" can't anchor
# a match. Kept tiny + accent-stripped (normalize() runs first).
_STOP = frozenset({
    "och", "i", "av", "en", "ett", "de", "med", "for", "pa", "till", "vid",
    "som", "ar", "den", "det",
})

# Conservative definite/plural suffix set, longest-first. Stripping requires >= 5
# chars to remain so roots like "proton"/"neutron"/"amplitud" are never damaged;
# applied SYMMETRICALLY to both the SVG token and the concept token so e.g.
# "isotoper" and "Isotop" meet at "isotop". (The bare "-n"/"-t"/"-a" definite
# endings are deliberately NOT here — they over-strip roots; those few cases are
# handled by FYSIK_ALIAS instead.)
_SUFFIXES = ("erna", "orna", "arna", "en", "et", "er", "or", "ar", "na")


def normalize(s: Optional[str]) -> str:
    """Accent-fold + lowercase + strip punctuation/parentheticals to a match key.

    NFKD-decompose and drop combining marks (å/ä/ö → a/a/o), lowercase, remove any
    ``(...)`` qualifier, turn separators into spaces, drop non-alphanumerics, and
    collapse whitespace. Deterministic and pure.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"\([^)]*\)", "", s)          # drop "(α, β, γ)" / "(Bohr-modell)"
    s = re.sub(r"[—–\-_:/]+", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _stem(word: str) -> str:
    """Strip one longest conservative definite/plural suffix (>= 5 chars remain)."""
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 5:
            return word[: -len(suf)]
    return word


def stem_phrase(s: str) -> str:
    """Stem each word of a normalized phrase (so plural/definite forms align)."""
    return " ".join(_stem(w) for w in s.split())


def _sig_tokens(s: str) -> set[str]:
    """Significant (non-stopword, length>2) tokens of a stemmed phrase."""
    return {t for t in s.split() if t not in _STOP and len(t) > 2}


def svg_heading(path: os.PathLike | str) -> str:
    """The first ``<text>`` element's content — the SVG's title heading.

    The Claude SVGs render the concept title as the first ``<text>`` (y≈35, bold).
    Returns the collapsed text, or ``""`` if there is none. Reads as UTF-8.
    """
    text = Path(path).read_text(encoding="utf-8")
    m = re.search(r"<text\b[^>]*>(.*?)</text>", text, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def svg_title(path: os.PathLike | str) -> Optional[str]:
    """The ``<title>`` element content if present (some authoring tools add one)."""
    text = Path(path).read_text(encoding="utf-8")
    m = re.search(r"<title\b[^>]*>(.*?)</title>", text, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


def load_fysik_concepts(path: os.PathLike | str | None = None) -> list[dict]:
    """Load the larbild fysik concept list (begrepp + area authority).

    Defaults to :data:`DEFAULT_FYSIK_JSON`; overridable via the ``LARBILD_FYSIK_JSON``
    env var or an explicit path (so a test can pass a fixture). Each item keeps at
    least ``begrepp`` and ``area``. Raises ``FileNotFoundError`` with a clear hint
    if absent — the import cannot map fysik without it.
    """
    p = Path(path or os.environ.get("LARBILD_FYSIK_JSON") or DEFAULT_FYSIK_JSON)
    if not p.exists():
        raise FileNotFoundError(
            f"larbild fysik concept set not found at {p}. Pass a path, set "
            f"LARBILD_FYSIK_JSON, or place the dump at {DEFAULT_FYSIK_JSON}."
        )
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{p}: expected a JSON list of concept objects")
    return data


def _build_index(concepts: Iterable[dict]) -> tuple[dict, dict]:
    """Return (by_norm, by_stem) concept indices keyed on the normalized begrepp.

    First-wins on a key collision so the index is deterministic over the input
    order. Each value is the original concept dict (carrying ``area``).
    """
    by_norm: dict[str, dict] = {}
    by_stem: dict[str, dict] = {}
    for c in concepts:
        b = c.get("begrepp")
        if not b:
            continue
        n = normalize(b)
        by_norm.setdefault(n, c)
        by_stem.setdefault(stem_phrase(n), c)
    return by_norm, by_stem


def resolve_fysik(
    subdir: str,
    name: str,
    heading: str,
    concepts: Iterable[dict],
    *,
    _index: Optional[tuple[dict, dict]] = None,
) -> Optional[dict]:
    """Resolve one fysik SVG to its larbild concept, or ``None`` (the gap).

    ``subdir``/``name`` are the SVG's path parts (its stable id is
    ``f"{subdir}/{name}"``); ``heading`` is :func:`svg_heading`. ``concepts`` is the
    larbild fysik concept list (:func:`load_fysik_concepts`). Resolution order, most
    confident first:

      1. **alias** — a hand-verified override in :data:`FYSIK_ALIAS` (incl. an
         explicit ``None`` = honest unmap).
      2. **exact** — the normalized filename slug or heading equals a begrepp.
      3. **stem** — the stemmed phrase equals a stemmed begrepp (plural/definite).
      4. **token-anchor** — a begrepp whose every significant token is a subset of
         the SVG candidate's tokens, AND whose ``area`` is allowed for this subdir
         (:data:`SUBDIR_AREA`). The most-specific (longest) such begrepp wins.

    Returns the matched concept dict (so the caller reads its authoritative
    ``area``) or ``None``. Pure / deterministic. ``_index`` is an optional
    prebuilt ``(by_norm, by_stem)`` so a batch call doesn't rebuild it per SVG.
    """
    key = f"{subdir}/{name}"
    by_norm, by_stem = _index if _index is not None else _build_index(concepts)

    # 1. alias (verified) — incl. forced unmap (None).
    if key in FYSIK_ALIAS:
        alias = FYSIK_ALIAS[key]
        if alias is None:
            return None
        return by_norm.get(normalize(alias))

    cand_strs = [normalize(name.replace("_", " ")), normalize(heading)]

    # 2. exact.
    for cs in cand_strs:
        if cs and cs in by_norm:
            return by_norm[cs]

    # 3. stemmed exact.
    for cs in cand_strs:
        if not cs:
            continue
        sp = stem_phrase(cs)
        if sp in by_stem:
            return by_stem[sp]

    # 4. area-scoped full-token-subset (most specific wins).
    allowed = SUBDIR_AREA.get(subdir, frozenset())
    best: Optional[tuple[dict, int]] = None
    concept_list = list(concepts)
    for cs in cand_strs:
        cand_tokens = _sig_tokens(stem_phrase(cs))
        if not cand_tokens:
            continue
        for c in concept_list:
            if allowed and c.get("area") not in allowed:
                continue
            cn = normalize(c.get("begrepp"))
            ctoks = _sig_tokens(stem_phrase(cn))
            if ctoks and ctoks <= cand_tokens:
                specificity = len(cn)          # prefer the longest concept name
                if best is None or specificity > best[1]:
                    best = (c, specificity)
    return best[0] if best else None


def iter_svgs(root: os.PathLike | str):
    """Yield ``(subdir, name, full_path)`` for every ``*.svg`` under ``root``.

    Sorted (subdir, name) for deterministic ordering. Skips the LICENSE.txt and any
    non-svg sibling. ``subdir`` is the immediate parent dir name (the topic group).
    """
    root = Path(root)
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        for svg in sorted(sub.glob("*.svg")):
            yield sub.name, svg.stem, svg
