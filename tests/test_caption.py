"""Unit tests for the bildtext overlay (lb-lesson, app.caption).

CPU-only (Pillow) — burns a Swedish lower-third caption band onto a beat keyframe.
Covers: a real overlay writes a NEW *_cap.png (source untouched), an EMPTY caption
is a no-op copy (every beat still yields one image path), å/ä/ö don't crash the
wrap/fit, a very long caption degrades to <= MAX_LINES, and the font resolver is
tolerant of a font-less box (built-in fallback).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from PIL import Image

from app.caption import (
    CaptionError,
    overlay_caption,
    resolve_font_path,
)


def _png(path: Path, size=(640, 360), colour=(40, 60, 90)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path)
    return path


def test_overlay_writes_new_cap_png_and_leaves_source(tmp_path):
    src = _png(tmp_path / "kf.png")
    before = src.read_bytes()
    out = overlay_caption(src, "Ljud är en tryckvåg i luften.")
    assert out == src.with_name("kf_cap.png")
    assert out.is_file()
    assert out != src
    # source image is never mutated in place
    assert src.read_bytes() == before
    # the captioned image opens + is the same dimensions
    img = Image.open(out)
    assert img.size == (640, 360)


def test_overlay_explicit_dest(tmp_path):
    src = _png(tmp_path / "kf.png")
    dest = tmp_path / "sub" / "captioned.png"
    out = overlay_caption(src, "Frekvens mäts i hertz.", dest)
    assert out == dest
    assert dest.is_file()


def test_empty_caption_is_noop_copy(tmp_path):
    # a beat may carry no bildtext at a given level — still produce one image path
    src = _png(tmp_path / "kf.png")
    out = overlay_caption(src, "   ")
    assert out.is_file()
    assert Image.open(out).size == (640, 360)


def test_swedish_glyphs_do_not_crash(tmp_path):
    src = _png(tmp_path / "kf.png")
    out = overlay_caption(src, "Återkoppling: ljudvågen färdas genom luften och når örat.")
    assert out.is_file()


def test_very_long_caption_degrades_gracefully(tmp_path):
    # a long välutvecklad caption must shrink/clamp, not overflow or raise
    src = _png(tmp_path / "kf.png", size=(800, 450))
    long = ("Ljud är en longitudinell tryckvåg som uppstår när ett föremål "
            "vibrerar och får luftens molekyler att förtätas och förtunnas i ett "
            "mönster som breder ut sig från källan tills vågen når trumhinnan.")
    out = overlay_caption(src, long)
    assert out.is_file()
    assert Image.open(out).size == (800, 450)


def test_overlay_missing_source_raises(tmp_path):
    with pytest.raises(CaptionError):
        overlay_caption(tmp_path / "nope.png", "hej")


def test_resolve_font_path_is_str_or_none():
    # tolerant of a font-less box — None means fall back to Pillow's built-in
    p = resolve_font_path()
    assert p is None or isinstance(p, str)


def test_caption_font_env_override(tmp_path, monkeypatch):
    # a bogus explicit font path is ignored (falls through to the candidate list /
    # built-in) — the overlay never hard-fails for a bad LARBILD_CAPTION_FONT
    monkeypatch.setenv("LARBILD_CAPTION_FONT", str(tmp_path / "not-a-font.ttf"))
    src = _png(tmp_path / "kf.png")
    out = overlay_caption(src, "Test")
    assert out.is_file()
