"""Tests for the authoritative SFW isolation CI guard (lb-5sx, P0.6).

Verifies the acceptance criteria: the guard exits 0 on the clean tree, exits
non-zero when a deliberate NSFW/imagegen leak is present, distinguishes a real
leak from honest attribution/documentation, and is callable as a plain script
(so the merge gate is one shell step).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import scripts.sfw_guard as guard

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── the clean tree passes ────────────────────────────────────────────────────


def test_guard_passes_on_clean_repo():
    """The real app/ + static/ tree has no NSFW/imagegen leak."""
    findings = guard.scan(REPO_ROOT)
    assert findings == [], (
        "SFW guard found leaks in the clean tree:\n"
        + "\n".join(f"  {f.path}:{f.line} [{f.rule}] {f.token}" for f in findings)
    )


def test_guard_script_exit_zero_on_clean_repo():
    """Runnable as a script (the CI gate form): exits 0 on the clean tree."""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "sfw_guard.py")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr


# ── a deliberate leak fails ──────────────────────────────────────────────────


def _write(tmp_path: Path, rel: str, text: str) -> None:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_nsfw_module_import_fails(tmp_path):
    """An NSFW/face-swap module import in app/ is a leak."""
    _write(tmp_path, "app/x.py", "import insightface\n")
    findings = guard.scan(tmp_path)
    assert any(f.rule == "nsfw-module-import" and f.token == "insightface"
               for f in findings)


def test_imagegen_import_fails(tmp_path):
    """A genuine import of imagegen's package is a leak (merge/copy-paste)."""
    _write(tmp_path, "app/x.py", "from imagegen.video import assemble\n")
    findings = guard.scan(tmp_path)
    assert any(f.rule == "imagegen-import" for f in findings)


def test_nsfw_asset_filename_fails(tmp_path):
    """A restricted asset filename (NSFW token + .safetensors) is a leak."""
    _write(tmp_path, "app/x.py", 'CKPT = "lustify-v6.safetensors"\n')
    findings = guard.scan(tmp_path)
    assert any(f.rule == "nsfw-asset-filename" and f.token == "lustify"
               for f in findings)


def test_nsfw_value_use_in_frontend_fails(tmp_path):
    """An NSFW LoRA name used as a JS string value (in static/) is a leak."""
    _write(tmp_path, "static/js/x.jsx", 'const lora = "wai_nsfw";\n')
    findings = guard.scan(tmp_path)
    assert any(f.token == "wai_nsfw" for f in findings)


def test_guard_script_exit_one_on_leak(tmp_path):
    """The script form exits non-zero on a leak (so the merge gate blocks)."""
    _write(tmp_path, "app/x.py", "import roop\n")
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "sfw_guard.py"),
         "--root", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert "FAIL" in proc.stderr


# ── honest attribution / documentation / fixtures are NOT leaks ──────────────


def test_fork_attribution_is_not_a_leak(tmp_path):
    """A docstring crediting the imagegen fork is prose, not an import."""
    _write(tmp_path, "app/x.py",
           '"""A SFW-stripped fork of imagegen\'s sibbis-app/gpu_lock.py."""\n'
           "VALUE = 1\n")
    assert guard.scan(tmp_path) == []


def test_excludes_documentation_is_not_a_leak(tmp_path):
    """Prose stating the app imports nothing from imagegen must not fire."""
    _write(tmp_path, "app/x.py",
           "# This codebase imports nothing from imagegen and ships no nsfw model.\n")
    assert guard.scan(tmp_path) == []


def test_real_repo_keyspace_comment_is_clean():
    """The real jobs.py 'disjoint from imagegen's vm100:image_gen:*' comment is
    attribution prose (not an import) and must not be flagged."""
    # Sanity: the live file exists and the guard passes (covered by the
    # clean-repo test, asserted here explicitly for the keyspace line).
    jobs_text = (REPO_ROOT / "app" / "jobs.py").read_text(encoding="utf-8")
    assert "vm100:image_gen" in jobs_text  # the comment is present …
    assert guard.scan(REPO_ROOT) == []      # … and the guard still passes
