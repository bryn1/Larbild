"""Lesson manuscript data model — the structured pedagogical script (lb-lesson).

A *Lärbild* lesson is a per-*delområde* (sub-area) OVERVIEW video that walks ALL
of an area's *begrepp* at one school level, multimodal: per-concept image +
level-scaled *bildtext* (caption) + *berättarröst* (narration) tying the concepts
together step by step. The PEDAGOGICAL CONTENT (which begrepp, how to explain
each at each level, the everyday examples, the connections) is generated OFFLINE
by an Opus agent (the skola-begrepp pattern) — so this module does NOT generate
anything. It defines the **manuscript**: the documented, validated JSON contract
the Opus step produces and the assembly (lb-lesson assembly) consumes.

────────────────────────────────────────────────────────────────────────────
THE MANUSCRIPT JSON SCHEMA (what the Opus step emits, one object per request)
────────────────────────────────────────────────────────────────────────────

    {
      "subject":  "fysik",            # taxonomy subject slug (LGR22 SoT)
      "area":     "ljud",             # taxonomy area slug, scoped to subject (the delområde)
      "level":    "enkel",            # enkel | utvecklad | valutvecklad
                                      #   (the school assessment levels; omit / null
                                      #    for a family short that COMPARES concepts)
      "kind":     "lesson",           # "lesson" (per-delområde overview) | "short"
                                      #   (per-begrepp or family short). Optional;
                                      #   defaults to "lesson". Drives the library kind.
      "title":    "Ljud — översikt",  # optional human title; a default is derived
      "narration_voice": "sv-standard",   # sv-standard | sv-lattlast (optional)
      "duration_target_s": 420,       # advisory target length (5-10 min overview);
                                      #   the real length follows the narration
      "beats": [                      # ORDERED beats — one per begrepp (or family member)
        {
          "begrepp":          "Ljudvåg",                 # the concept name (required)
          "forklaring":       "Ljud är en våg som ...",  # the Förklaring (skola shape)
          "vardagligt_exempel":"När du knäpper med ...", # the Vardagligt exempel (skola shape)
          "image_prompt":     "en ljudvåg som sprider sig i luften, enkel pedagogisk illustration",
          "caption": {                # the bildtext, scaled per level (short/longer/long):
            "short":  "Ljud är en våg.",                      # enkel
            "longer": "Ljud är en våg som rör sig i luften.", # utvecklad
            "long":   "Ljud är en tryckvåg som breder ut sig i luften från en källa."  # valutvecklad
          },
          "narration":  "Vi börjar med ljudvågen. Ljud är ...",  # the spoken talking-point line
          "connection": "Den här vågen fångas upp av örat — nästa begrepp.",  # link to the NEXT beat (optional)
          "motion":     "still",      # "still" (Ken-Burns over the image) |
                                      #   "short_clip" (a short i2v clip for the beat)
          "image_type": "illustration",   # optional override of the per-beat imageType
                                          #   (drives the SFW LoRA); defaults to "illustration"
          "duration_s": 28.0          # optional explicit hold; else derived from the
                                      #   narration length (so the voice never gets cut)
        },
        ...  # one beat per begrepp in the delområde
      ]
    }

Notes for the Opus author:
  * ``caption`` may be a flat string (used at every level) OR the {short,longer,
    long} object. For a lesson the object is preferred — the three keys ARE the
    three levels (short=enkel, longer=utvecklad, long=valutvecklad). When the
    manuscript carries a single ``level`` the assembly picks the matching caption
    rung; a flat string is used as-is. (Producing one manuscript per level, OR
    one manuscript with all three caption rungs, both work — see
    :func:`Lesson.for_level`.)
  * ``connection`` is the step-by-step glue; it is spoken as part of THIS beat's
    narration tail so concepts are explicitly tied together. Optional on the last
    beat.
  * A per-*begrepp* short is just a one-beat (or few-beat) manuscript with
    ``kind="short"``. A FAMILY short (e.g. ljus → konvex vs konkav spegel/lins)
    is a multi-beat ``kind="short"`` manuscript whose beats are the related
    concepts being compared — usually with ``level`` null (it contrasts rather
    than scaling one explanation across levels).

This module is pure data + validation (no I/O, no GPU, no DB) so it is unit-
testable in isolation. SFW-only: it carries pedagogical text + neutral image
prompts; no restricted field, content enum, model, or reference.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "LessonError",
    "LEVELS",
    "LEVEL_TO_CAPTION_RUNG",
    "CAPTION_RUNGS",
    "MOTIONS",
    "Caption",
    "Beat",
    "Lesson",
]


class LessonError(ValueError):
    """A manuscript was malformed (missing/blank field, bad level/motion, …)."""


# The three school assessment levels (the Skolverket E/C/A bands, but NEVER shown
# as letters in a student-facing context — the skola-rig rule). Stored slug form
# uses ASCII 'valutvecklad' (no å) to stay filename/DB-key safe; the UI renders
# the display label.
LEVELS = ("enkel", "utvecklad", "valutvecklad")

# The caption (bildtext) rungs, short → long, and how a level maps to a rung:
# short=enkel, longer=utvecklad, long=valutvecklad (the owner spec's
# "korta/längre/långa bildtexter" tied to "enkel/utvecklad/välutvecklad nivå").
CAPTION_RUNGS = ("short", "longer", "long")
LEVEL_TO_CAPTION_RUNG = {
    "enkel": "short",
    "utvecklad": "longer",
    "valutvecklad": "long",
}

# A beat's image is either a held still (Ken-Burns in the mux) or a short i2v
# clip generated for that beat. 'short_clip' is honoured by the assembly only
# when i2v is wired + the GPU is free; it degrades to 'still' otherwise.
MOTIONS = ("still", "short_clip")

# Defaults the assembly falls back to so a sparse manuscript still renders.
_DEFAULT_IMAGE_TYPE = "illustration"
_DEFAULT_MOTION = "still"
_DEFAULT_VOICE = "sv-standard"
_DEFAULT_KIND = "lesson"
# Per-beat hold when neither an explicit duration nor a narration length is known
# (a floor so a captioned still is never a single frame).
_MIN_BEAT_S = 6.0


def _norm_level(level: Optional[str]) -> Optional[str]:
    """Normalise a level slug, accepting the å spelling. None passes through."""
    if level is None:
        return None
    s = str(level).strip().lower()
    if not s:
        return None
    s = s.replace("ä", "a").replace("å", "a").replace("ö", "o")
    if s not in LEVELS:
        raise LessonError(
            f"unknown level {level!r}; valid: {', '.join(LEVELS)} (or null)"
        )
    return s


@dataclass(frozen=True)
class Caption:
    """A bildtext at up to three rungs (short / longer / long).

    A manuscript may give a flat string (same text at every rung) or the three
    rungs. :meth:`for_level` returns the text for a school level, falling back up
    the rungs (so a missing 'long' uses 'longer' then 'short') — text is never
    blank for a present caption.
    """

    short: str = ""
    longer: str = ""
    long: str = ""

    @classmethod
    def parse(cls, raw: Any) -> "Caption":
        if raw is None:
            return cls()
        if isinstance(raw, str):
            t = raw.strip()
            return cls(short=t, longer=t, long=t)
        if isinstance(raw, dict):
            s = str(raw.get("short", "") or "").strip()
            lo = str(raw.get("longer", "") or "").strip()
            ln = str(raw.get("long", "") or "").strip()
            # Backfill empties so any rung resolves: long←longer←short and
            # short→longer→long, then symmetric fill.
            s = s or lo or ln
            lo = lo or s or ln
            ln = ln or lo or s
            return cls(short=s, longer=lo, long=ln)
        raise LessonError(
            "caption must be a string or an object with short/longer/long"
        )

    def for_level(self, level: Optional[str]) -> str:
        """The caption text for a school level (None/unknown → the 'longer' rung)."""
        rung = LEVEL_TO_CAPTION_RUNG.get(level or "", "longer")
        return getattr(self, rung) or self.longer or self.short or self.long

    def is_empty(self) -> bool:
        return not (self.short or self.longer or self.long)

    def as_dict(self) -> dict:
        return {"short": self.short, "longer": self.longer, "long": self.long}


@dataclass(frozen=True)
class Beat:
    """One beat of a lesson — a single *begrepp*, fully specified for assembly.

    Couples to the skola begrepp shape ``|Begrepp | Förklaring | Vardagligt
    exempel|`` (the first three fields), plus the multimodal render spec:
    ``image_prompt`` (what the keyframe shows), ``caption`` (the level-scaled
    bildtext overlaid on it), ``narration`` (the spoken talking-point), and
    ``connection`` (the step-by-step link to the next concept, spoken as this
    beat's narration tail). ``motion`` picks still-with-Ken-Burns vs a short i2v
    clip; ``image_type`` drives the SFW LoRA. ``index`` is the beat order.
    """

    index: int
    begrepp: str
    forklaring: str = ""
    vardagligt_exempel: str = ""
    image_prompt: str = ""
    caption: Caption = field(default_factory=Caption)
    narration: str = ""
    connection: str = ""
    motion: str = _DEFAULT_MOTION
    image_type: str = _DEFAULT_IMAGE_TYPE
    duration_s: Optional[float] = None

    @classmethod
    def parse(cls, raw: Any, index: int) -> "Beat":
        if not isinstance(raw, dict):
            raise LessonError(f"beat {index} must be an object")
        begrepp = str(raw.get("begrepp", "") or "").strip()
        if not begrepp:
            raise LessonError(f"beat {index}: 'begrepp' is required")

        motion = str(raw.get("motion", _DEFAULT_MOTION) or _DEFAULT_MOTION).strip()
        if motion not in MOTIONS:
            raise LessonError(
                f"beat {index}: unknown motion {motion!r}; valid: {', '.join(MOTIONS)}"
            )
        image_type = str(raw.get("image_type", "") or "").strip() or _DEFAULT_IMAGE_TYPE

        dur = raw.get("duration_s")
        if dur is not None:
            try:
                dur = float(dur)
            except (TypeError, ValueError):
                raise LessonError(f"beat {index}: duration_s must be a number")
            if dur <= 0:
                raise LessonError(f"beat {index}: duration_s must be > 0")

        return cls(
            index=index,
            begrepp=begrepp,
            forklaring=str(raw.get("forklaring", "") or "").strip(),
            vardagligt_exempel=str(raw.get("vardagligt_exempel", "") or "").strip(),
            image_prompt=str(raw.get("image_prompt", "") or "").strip(),
            caption=Caption.parse(raw.get("caption")),
            narration=str(raw.get("narration", "") or "").strip(),
            connection=str(raw.get("connection", "") or "").strip(),
            motion=motion,
            image_type=image_type,
            duration_s=dur,
        )

    def caption_for(self, level: Optional[str]) -> str:
        """The bildtext to overlay for this beat at ``level`` (may be empty)."""
        return self.caption.for_level(level)

    def narration_for(self, level: Optional[str]) -> str:
        """The full spoken line for this beat: the talking-point + the connection.

        The ``connection`` is appended so concepts are explicitly tied together
        step by step in the voice-over. If the beat has no explicit ``narration``,
        a neutral line is scaffolded from the begrepp + förklaring so the beat is
        never silent. ``level`` is accepted for symmetry / future per-level
        narration tuning; today the spoken line is level-independent (the
        per-level differentiation lives in the caption rung + the manuscript the
        Opus step wrote per level).
        """
        line = self.narration or self._scaffold_narration()
        if self.connection:
            line = f"{line} {self.connection}".strip()
        return line

    def _scaffold_narration(self) -> str:
        parts = [self.begrepp + "."]
        if self.forklaring:
            parts.append(self.forklaring)
        if self.vardagligt_exempel:
            parts.append(self.vardagligt_exempel)
        return " ".join(parts).strip()

    def image_prompt_or_scaffold(self, subject: str, area: str) -> str:
        """The image prompt for this beat, or a neutral educational scaffold.

        Never names a LoRA (the LoRA is resolved backend-side from subject +
        image_type — the SFW guard); a missing prompt becomes a neutral
        educational description of the begrepp.
        """
        if self.image_prompt:
            return self.image_prompt
        base = f"educational {self.image_type} for {subject} ({area}): {self.begrepp}"
        if self.forklaring:
            base += f" — {self.forklaring}"
        return base

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "begrepp": self.begrepp,
            "forklaring": self.forklaring,
            "vardagligt_exempel": self.vardagligt_exempel,
            "image_prompt": self.image_prompt,
            "caption": self.caption.as_dict(),
            "narration": self.narration,
            "connection": self.connection,
            "motion": self.motion,
            "image_type": self.image_type,
            "duration_s": self.duration_s,
        }


@dataclass(frozen=True)
class Lesson:
    """A parsed, validated lesson manuscript — the assembly's input.

    ``kind`` is the library artefact kind: ``'lesson'`` (a per-delområde overview)
    or ``'short'`` (a per-begrepp / family short). ``level`` is the school level
    (None for a family short). ``beats`` are the ordered begrepp. The class is
    constructed via :meth:`parse` from the documented manuscript JSON and never
    holds invalid data (parse raises :class:`LessonError`).
    """

    subject: str
    area: str
    kind: str = _DEFAULT_KIND
    level: Optional[str] = None
    title: Optional[str] = None
    narration_voice: str = _DEFAULT_VOICE
    duration_target_s: Optional[int] = None
    beats: tuple[Beat, ...] = ()

    # ── parsing ──────────────────────────────────────────────────────────
    @classmethod
    def parse(cls, raw: Any) -> "Lesson":
        """Parse + validate a manuscript dict into a :class:`Lesson`.

        Raises :class:`LessonError` on any structural problem (missing subject/
        area, no beats, a bad level/kind/motion, a beat without a begrepp). Does
        NOT validate subject/area/level against the taxonomy SoT or the narration
        voice list — that is the route/assembly layer's job (it owns the taxonomy
        import); keeping this layer taxonomy-free makes it trivially unit-testable
        and reusable. ``kind`` defaults to 'lesson'.
        """
        if not isinstance(raw, dict):
            raise LessonError("manuscript must be a JSON object")

        subject = str(raw.get("subject", "") or "").strip()
        area = str(raw.get("area", "") or "").strip()
        if not subject or not area:
            raise LessonError("manuscript needs 'subject' and 'area'")

        kind = str(raw.get("kind", _DEFAULT_KIND) or _DEFAULT_KIND).strip().lower()
        if kind not in ("lesson", "short"):
            raise LessonError(f"manuscript kind must be 'lesson' or 'short', got {kind!r}")

        level = _norm_level(raw.get("level"))

        voice = str(raw.get("narration_voice", "") or "").strip() or _DEFAULT_VOICE

        target = raw.get("duration_target_s")
        if target is not None:
            try:
                target = int(target)
            except (TypeError, ValueError):
                raise LessonError("duration_target_s must be an integer")

        raw_beats = raw.get("beats")
        if not isinstance(raw_beats, list) or not raw_beats:
            raise LessonError("manuscript needs a non-empty 'beats' list")
        beats = tuple(Beat.parse(b, i) for i, b in enumerate(raw_beats))

        title = str(raw.get("title", "") or "").strip() or None

        return cls(
            subject=subject,
            area=area,
            kind=kind,
            level=level,
            title=title,
            narration_voice=voice,
            duration_target_s=target,
            beats=beats,
        )

    # ── derived views ────────────────────────────────────────────────────
    @property
    def begrepp_count(self) -> int:
        return len(self.beats)

    def display_title(self) -> str:
        """A non-blank title: the explicit one, else derived from area + level."""
        if self.title:
            return self.title
        base = f"{self.area} — översikt" if self.kind == "lesson" else f"{self.area} — kort"
        if self.level:
            base += f" ({self.level})"
        return base

    def for_level(self, level: str) -> "Lesson":
        """A copy of this manuscript pinned to ``level``.

        Used to produce the three level-versions of one delområde from a single
        all-rung manuscript: the beats are unchanged (the per-level differences
        live in each caption's rung + the level-independent narration), only the
        lesson's ``level`` flips — so the assembly overlays the matching caption
        rung and tags the library video with the level. Raises on a bad level.
        """
        lvl = _norm_level(level)
        if lvl is None:
            raise LessonError("for_level requires a concrete level")
        return Lesson(
            subject=self.subject, area=self.area, kind=self.kind, level=lvl,
            title=self.title, narration_voice=self.narration_voice,
            duration_target_s=self.duration_target_s, beats=self.beats,
        )

    def narration_script(self) -> str:
        """The full continuous voice-over: every beat's spoken line, in order.

        This is the *berättarröst that ties the content together* — the per-beat
        talking-points (each with its connection tail) joined into one script.
        The assembly synthesises each beat separately (to time the captions) and
        concatenates the audio; this joined form is handy for a single-shot
        narration preview or a manuscript sanity check.
        """
        return "\n".join(b.narration_for(self.level) for b in self.beats).strip()

    def as_dict(self) -> dict:
        return {
            "subject": self.subject,
            "area": self.area,
            "kind": self.kind,
            "level": self.level,
            "title": self.title,
            "narration_voice": self.narration_voice,
            "duration_target_s": self.duration_target_s,
            "begrepp_count": self.begrepp_count,
            "beats": [b.as_dict() for b in self.beats],
        }
