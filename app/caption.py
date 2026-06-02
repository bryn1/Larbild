"""Caption (bildtext) overlay — Swedish label band on a beat keyframe (lb-lesson).

Each lesson beat shows a *bildtext* — the short/longer/long caption that, in the
owner's words, "förklarar vad som händer" — burned onto the beat's keyframe as a
lower-third label (the label-overlay approach: a translucent band + wrapped
Swedish text). The captioned image is then what the mux pans (Ken-Burns) while
the beat's narration plays over it.

This module is the overlay step. It is **CPU-only** (Pillow, no GPU, no network),
so it runs anywhere the assembly's tests run. Pillow is already a dependency
(thumbnails / pnginfo). The font is resolved from a small list of common Swedish-
glyph-capable TrueType fonts (DejaVu / Liberation / Noto), falling back to
Pillow's built-in bitmap font if none is installed — so the overlay NEVER fails
for lack of a font (it just renders smaller, ASCII-safe text). The overlay
function is also injectable at the assembly seam, so a test can stub it out
entirely and assert the beat-clip pipeline without producing real pixels.

Design choices:
  * **Lower third, translucent band.** A dark, semi-opaque band across the bottom
    ~28% keeps the caption legible over any keyframe without hiding the subject.
  * **Word-wrap to the band width**, longest-line-aware font sizing, max ~3 lines
    (long captions for välutvecklad shrink to fit rather than overflow).
  * **Swedish-safe.** å/ä/ö render via the resolved TTF; the band + text are the
    only marks added (no restricted glyph, watermark, or content).
  * **Idempotent output path.** Writes a new ``*_cap.png`` beside the source by
    default (never mutates the rendered keyframe in place — the original stays in
    the library / cache).

SFW-only: draws an educational caption band; no restricted import, font asset,
or content.

Env overrides:
  LARBILD_CAPTION_FONT        explicit TTF path (else the search list)
  LARBILD_CAPTION_BAND_FRAC   default 0.28  band height as a fraction of image H
  LARBILD_CAPTION_MAX_LINES   default 3
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional, Sequence

from PIL import Image, ImageDraw, ImageFont

__all__ = [
    "CaptionError",
    "OverlayFn",
    "overlay_caption",
    "resolve_font_path",
]


class CaptionError(RuntimeError):
    """The caption could not be overlaid (unreadable image, write failure)."""


# Injected overlay signature: (src_image, caption_text, dest_path) -> dest_path.
# The assembly calls this per beat; the default is :func:`overlay_caption`, a
# test passes a stub that just records the call + writes a marker file.
OverlayFn = Callable[[Path, str, Path], Path]

BAND_FRAC = float(os.environ.get("LARBILD_CAPTION_BAND_FRAC", "0.28"))
MAX_LINES = int(os.environ.get("LARBILD_CAPTION_MAX_LINES", "3"))

# Common TrueType fonts that carry the Swedish å/ä/ö glyphs, in preference order.
# DejaVu ships on Debian/Ubuntu (the Sibbis + CI base); Liberation/Noto are
# fallbacks. If none resolve, Pillow's built-in bitmap font is used (ASCII-ish,
# but the overlay still succeeds — never a hard failure on a font-less box).
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
)


def resolve_font_path() -> Optional[str]:
    """The first available Swedish-capable TTF path, or None (→ built-in font).

    Honours ``LARBILD_CAPTION_FONT`` first (an explicit override), then the
    candidate list. Returns None when nothing is found so the caller falls back
    to :func:`PIL.ImageFont.load_default`.
    """
    explicit = os.environ.get("LARBILD_CAPTION_FONT")
    if explicit and Path(explicit).is_file():
        return explicit
    for cand in _FONT_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return None


def _load_font(size: int) -> ImageFont.ImageFont:
    path = resolve_font_path()
    if path is not None:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:  # noqa: BLE001 — corrupt/unsupported TTF → built-in
            pass
    # Pillow >=10 load_default(size) accepts a size; older ignores it. Guard both.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


def _text_w(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    """Pixel width of a single line, across Pillow versions."""
    try:
        l, t, r, b = draw.textbbox((0, 0), text, font=font)
        return int(r - l)
    except Exception:  # pragma: no cover - ancient Pillow
        return int(draw.textlength(text, font=font))


def _line_h(draw: ImageDraw.ImageDraw, font) -> int:
    try:
        l, t, r, b = draw.textbbox((0, 0), "ÅgMjpQ", font=font)
        return int(b - t)
    except Exception:  # pragma: no cover
        return font.size if hasattr(font, "size") else 12


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    """Greedy word-wrap ``text`` to ``max_w`` pixels. Never drops a word.

    A single word longer than ``max_w`` is kept on its own line (it will be
    shrunk by the caller's font-fit loop, or simply overrun slightly — better
    than dropping content).
    """
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    cur = words[0]
    for w in words[1:]:
        trial = f"{cur} {w}"
        if _text_w(draw, trial, font) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def _fit_font(draw, text: str, max_w: int, band_h: int,
              start: int, min_size: int) -> tuple[object, list[str]]:
    """Pick the largest font size whose wrapped text fits the band.

    Shrinks from ``start`` down to ``min_size`` until the wrapped caption is at
    most :data:`MAX_LINES` lines AND fits the band height. At ``min_size`` it
    accepts whatever wraps (truncating to MAX_LINES with an ellipsis) so a very
    long caption degrades gracefully rather than overflowing the frame.
    """
    size = start
    while size >= min_size:
        font = _load_font(size)
        lines = _wrap(draw, text, font, max_w)
        lh = _line_h(draw, font)
        total_h = lh * len(lines) + (len(lines) - 1) * max(2, lh // 4)
        if len(lines) <= MAX_LINES and total_h <= band_h:
            return font, lines
        size -= max(2, start // 16)
    # Floor: accept min_size, clamp to MAX_LINES with an ellipsis on the last.
    font = _load_font(min_size)
    lines = _wrap(draw, text, font, max_w)
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        lines[-1] = (lines[-1].rstrip(".") + " …")
    return font, lines


def _caption_dest(src: Path) -> Path:
    return src.with_name(src.stem + "_cap" + src.suffix)


def overlay_caption(
    src: Path,
    caption: str,
    dest: Optional[Path] = None,
    *,
    band_frac: float = BAND_FRAC,
) -> Path:
    """Burn ``caption`` as a lower-third label onto the keyframe at ``src``.

    Reads the rendered keyframe, draws a translucent dark band across the bottom
    ``band_frac`` of the frame, wraps + size-fits the Swedish caption into it
    (white text, max :data:`MAX_LINES` lines), and writes the result to ``dest``
    (default ``<src>_cap.png``). The source image is never modified in place.
    Returns the destination path.

    An EMPTY caption is a no-op-with-copy: the keyframe is copied to ``dest``
    unchanged (a beat may legitimately carry no bildtext at a given level), so the
    assembly can always consume a single per-beat image path. Raises
    :class:`CaptionError` if the source can't be opened or the result can't be
    written.
    """
    src = Path(src)
    dest = Path(dest) if dest is not None else _caption_dest(src)
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        base = Image.open(src).convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise CaptionError(f"cannot open keyframe {src}: {e}") from e

    text = (caption or "").strip()
    if not text:
        # No bildtext at this level — keep the frame, just write it to dest.
        try:
            base.save(dest, "PNG")
        except Exception as e:  # noqa: BLE001
            raise CaptionError(f"cannot write {dest}: {e}") from e
        return dest

    W, H = base.size
    band_h = max(1, int(H * band_frac))
    pad = max(8, W // 60)

    # Translucent band (RGBA composited onto the RGB base).
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle([0, H - band_h, W, H], fill=(0, 0, 0, 165))

    inner_w = W - 2 * pad
    inner_h = band_h - 2 * pad
    start_size = max(14, int(band_h * 0.42))
    min_size = max(10, int(band_h * 0.16))
    font, lines = _fit_font(od, text, inner_w, inner_h, start_size, min_size)

    lh = _line_h(od, font)
    gap = max(2, lh // 4)
    block_h = lh * len(lines) + gap * (len(lines) - 1)
    y = (H - band_h) + (band_h - block_h) // 2
    for line in lines:
        lw = _text_w(od, line, font)
        x = (W - lw) // 2
        # A subtle shadow lifts the text off busy frames, then the white glyph.
        od.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0, 220))
        od.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += lh + gap

    composited = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    try:
        composited.save(dest, "PNG")
    except Exception as e:  # noqa: BLE001
        raise CaptionError(f"cannot write {dest}: {e}") from e
    return dest
