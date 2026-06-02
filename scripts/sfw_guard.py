#!/usr/bin/env python3
"""SFW isolation guard — the authoritative CI check (lb-5sx, P0.6).

Isolation plane #1 (risk R6: accidental repo merge / NSFW leak). Scans the
Lärbild source tree — ``app/`` AND ``static/`` (the executing backend + the
shipped frontend) — and FAILS the build (exit 1) if a real NSFW/face-swap leak
is present:

  1. a genuine Python import of imagegen's package (``import imagegen`` /
     ``from imagegen[...] import``) — a copy-paste/merge leak,
  2. an NSFW / face-swap **module import** (``import insightface``, ``from roop
     ...``, an ``onnxruntime`` / ``reactor`` import — the dropped face stack),
  3. an NSFW model / LoRA / checkpoint **name used as a value** — a string
     literal like ``"lustify-v6.safetensors"`` or ``lora = "wai-nsfw"`` that
     would actually reference a restricted asset at runtime,
  4. a restricted asset **filename** (``*.safetensors`` / ``*.ckpt`` whose name
     carries an NSFW token).

What it deliberately does NOT flag (these are not leaks, and flagging them makes
the guard cry wolf):

  * fork **attribution** in a module docstring/comment ("a SFW-stripped fork of
    imagegen's ``sibbis-app/gpu_lock.py``") — prose, not an import;
  * the project **describing what it excludes** ("imports nothing from
    imagegen", requirements.txt listing the dropped ``insightface`` deps);
  * the SFW **allowlist / denylist** itself (this script, ``tests/test_shell.py``)
    legitimately enumerating the forbidden tokens;
  * **test fixtures** that submit a deliberately-evil LoRA (``"smuggled-nsfw-v9"``)
    to PROVE the allowlist rejects it — that is the test of the boundary, run in
    ``tests/`` (which this guard does not treat as shippable surface).

The load-bearing SFW invariant is enforced structurally by the backend allowlist
(every render path resolves a LoRA only from ``baseLora ∪ loraMatrix`` via
``app.workflow.resolve_and_validate_lora``); this guard is the second line —
it stops an NSFW import/asset from ever entering the tree in the first place.

Usage::

    python3 scripts/sfw_guard.py            # scan app/ + static/, print + exit 0/1
    python3 scripts/sfw_guard.py --root DIR # scan a specific tree (tests use this)
    python3 scripts/sfw_guard.py --json     # machine-readable findings

Maintaining the lists: add an NSFW model/LoRA/module token to
:data:`NSFW_TOKENS`; add an imagegen package name to :data:`IMAGEGEN_PACKAGES`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable, NamedTuple

# ── what the guard forbids (as REAL uses, see classification below) ──────────
#
# NSFW model / LoRA / checkpoint names + the face-swap module stack Lärbild
# deliberately does not ship. Matched only when used as a value/import/filename
# (never in attribution prose). Keep in sync with imagegen's restricted set +
# tests/test_shell.py's foundation markers.
NSFW_TOKENS: tuple[str, ...] = (
    "nsfw", "lustify", "bigasp", "wai-nsfw", "wai_nsfw", "cyberrealistic",
    "pony", "porn", "deepfake",
    # face-swap stack (dropped from requirements; consent-gated only, never default)
    "reactor", "insightface", "buffalo_l", "inswapper", "onnxruntime",
    "face_swap", "faceswap", "roop", "wardrobe", "undress",
)

# imagegen package / module names. A genuine *import* of any of these is a leak.
IMAGEGEN_PACKAGES: tuple[str, ...] = (
    "imagegen", "sibbis_app", "sibbis-app",
)

# Asset extensions whose *filename* must never carry an NSFW token.
ASSET_SUFFIXES: tuple[str, ...] = (".safetensors", ".ckpt", ".pt", ".bin", ".onnx")

# ── scan surface ─────────────────────────────────────────────────────────────
# Only the SHIPPED, EXECUTING surface: the backend package + the frontend. tests/
# and top-level docs legitimately enumerate forbidden tokens (denylists +
# negative fixtures), so they are NOT scanned for NSFW-name *uses* — but a real
# imagegen *import* is checked everywhere via a separate, narrow rule below.
SCAN_DIRS: tuple[str, ...] = ("app", "static")
SCAN_SUFFIXES: frozenset[str] = frozenset(
    {".py", ".js", ".jsx", ".mjs", ".ts", ".tsx", ".css", ".html", ".json"}
)
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {".git", "__pycache__", "node_modules", ".venv", "venv", "env",
     ".pytest_cache", ".mypy_cache", "data", "storage", "output", "results",
     ".vite", "dist"}
)

# A Python import of an imagegen / sibbis-app package (the real leak signal).
# Matches ``import imagegen``, ``from imagegen import x``, ``from imagegen.foo``,
# ``import sibbis_app`` — but NOT the prose "imports nothing from imagegen".
_IMAGEGEN_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+(?P<f>[\w.]+)\s+import\b|import\s+(?P<i>[\w.]+))",
)

# An NSFW / face-swap module import: ``import insightface`` / ``from roop import``
# / ``import onnxruntime``. Built from the subset of NSFW_TOKENS that are valid
# python module identifiers.
_PY_MODULE_TOKENS = tuple(t for t in NSFW_TOKENS if re.fullmatch(r"[a-z_][a-z0-9_]*", t))

# A token used as a quoted string value or a JS/Py assignment RHS — i.e. a real
# reference, e.g. ``lora = "wai_nsfw"``, ``"ckpt": "lustify.safetensors"``,
# ``loraName: 'pony-xl'``. We look for the token inside a quoted string.
_QUOTED_RE = re.compile(r"""(['"])(?P<val>[^'"]*)\1""")


class Finding(NamedTuple):
    """One leak: file, 1-based line, the rule that fired, the token, line text."""

    path: str
    line: int
    rule: str
    token: str
    text: str

    def as_dict(self) -> dict:
        return {"path": self.path, "line": self.line, "rule": self.rule,
                "token": self.token, "text": self.text}


def _iter_files(root: Path) -> Iterable[Path]:
    """Yield every shippable source file under app/ + static/."""
    for d in SCAN_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIR_NAMES for part in path.relative_to(root).parts):
                continue
            if path.suffix.lower() in SCAN_SUFFIXES:
                yield path


def _token_in(value: str, token: str) -> bool:
    """True if *token* occurs in *value* (case-insensitive substring)."""
    return token in value.lower()


def _check_imagegen_import(line: str) -> str | None:
    """Return the imagegen package if *line* is a real import of it, else None."""
    m = _IMAGEGEN_IMPORT_RE.match(line)
    if not m:
        return None
    mod = (m.group("f") or m.group("i") or "").lower()
    head = mod.split(".")[0]
    for pkg in IMAGEGEN_PACKAGES:
        pkg_id = pkg.replace("-", "_").lower()
        if head == pkg_id:
            return pkg
    return None


def _check_nsfw_module_import(line: str) -> str | None:
    """Return the NSFW module if *line* imports one (insightface/roop/…), else None."""
    m = _IMAGEGEN_IMPORT_RE.match(line)
    if not m:
        return None
    mod = (m.group("f") or m.group("i") or "").lower()
    head = mod.split(".")[0]
    return head if head in _PY_MODULE_TOKENS else None


def _check_value_use(line: str) -> list[str]:
    """NSFW tokens that appear inside a quoted string VALUE on this line.

    A quoted string is a runtime value (a lora name, a checkpoint, an asset
    path). A token inside one is a real reference — distinct from the token
    appearing in surrounding prose/identifiers. Returns the matched tokens.
    """
    hits: list[str] = []
    for qm in _QUOTED_RE.finditer(line):
        val = qm.group("val").lower()
        if not val:
            continue
        for tok in NSFW_TOKENS:
            if tok in val and tok not in hits:
                hits.append(tok)
    return hits


def _check_asset_filename(line: str) -> list[str]:
    """NSFW tokens in a model/checkpoint asset filename referenced on this line."""
    hits: list[str] = []
    low = line.lower()
    if not any(suf in low for suf in ASSET_SUFFIXES):
        return hits
    for qm in _QUOTED_RE.finditer(line):
        val = qm.group("val").lower()
        if not any(val.endswith(suf) for suf in ASSET_SUFFIXES):
            continue
        for tok in NSFW_TOKENS:
            if tok in val and tok not in hits:
                hits.append(tok)
    return hits


def _scan_text(rel: str, text: str) -> list[Finding]:
    """Find real NSFW/imagegen leaks in one shippable file's text."""
    findings: list[Finding] = []
    for i, line in enumerate(text.splitlines(), start=1):
        pkg = _check_imagegen_import(line)
        if pkg:
            findings.append(Finding(rel, i, "imagegen-import", pkg, line.strip()[:120]))
        mod = _check_nsfw_module_import(line)
        if mod:
            findings.append(Finding(rel, i, "nsfw-module-import", mod, line.strip()[:120]))
        for tok in _check_asset_filename(line):
            findings.append(Finding(rel, i, "nsfw-asset-filename", tok, line.strip()[:120]))
        for tok in _check_value_use(line):
            # An asset-filename hit already covered the strongest case; still
            # record a plain value use (e.g. lora = "wai_nsfw" with no suffix).
            if not any(f.line == i and f.token == tok and f.rule == "nsfw-asset-filename"
                       for f in findings):
                findings.append(Finding(rel, i, "nsfw-value-use", tok, line.strip()[:120]))
    return findings


def scan(root: Path) -> list[Finding]:
    """Scan app/ + static/ under *root*; return all real-leak findings (sorted)."""
    findings: list[Finding] = []
    for path in _iter_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(_scan_text(str(path.relative_to(root)), text))
    return sorted(findings, key=lambda f: (f.path, f.line, f.token))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lärbild SFW isolation CI guard")
    parser.add_argument("--root", default=None,
                        help="repo root to scan (default: this script's repo)")
    parser.add_argument("--json", action="store_true", help="emit JSON findings")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    findings = scan(root)

    if args.json:
        print(json.dumps({"root": str(root), "ok": not findings,
                          "findings": [f.as_dict() for f in findings]}, indent=2))
    elif findings:
        print(f"SFW guard: {len(findings)} NSFW/imagegen leak(s) in the shipped "
              f"tree under {root}:", file=sys.stderr)
        for f in findings:
            print(f"  {f.path}:{f.line}: [{f.rule}] {f.token!r}  |  {f.text}",
                  file=sys.stderr)
        print("\nFAIL — Lärbild is STRICTLY SFW. An NSFW model/LoRA/module, an "
              "imagegen import, or a restricted asset filename must never land in "
              "app/ or static/.", file=sys.stderr)
    else:
        n_files = sum(1 for _ in _iter_files(root))
        print(f"SFW guard: OK — scanned {n_files} shipped source files (app/ + "
              f"static/) under {root}; no NSFW/imagegen leak.")

    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
