"""Film templates — the 6 videoTypes → keyframe storyboard spec + LoRA map
(lb-1bh, P6.2).

A Lärbild film is a *storyboard of image keyframes* with a Swedish voice-over —
not a generative video model. Each of the taxonomy's 6 ``videoTypes`` (lb-582)
gets a **template**: the image type its keyframes are rendered as, a default
narration voice + shot count + per-shot duration, and a Ken-Burns motion style
for the mux. The image type is the load-bearing field: it feeds the SAME
backend-authoritative LoRA resolver the stills funnel uses
(:func:`app.taxonomy.resolve_lora` via :func:`app.workflow.resolve_and_validate_lora`),
so a film's frames resolve only **allowlisted** SFW LoRAs — the filmtype can
never name a LoRA, exactly like the stills path (the SFW injection guard).

This module is pure data + resolution: it builds the per-film
``(subject) → [keyframe specs]`` storyboard skeleton and the
``videoType → imageType → resolved LoRA`` map. It does NOT render, synthesise,
or mux — that is the pipeline (lb-4vw); keeping the templates a separate,
side-effect-free layer makes the mapping unit-testable without a GPU or VM600.

The mapping is **backend-authoritative**: ``FILM_TEMPLATES`` is defined here (not
accepted from a client), and :func:`resolve_film_lora` runs every resolved LoRA
through the workflow allowlist. An unknown ``videoType`` raises rather than
silently falling back.

SFW-only: educational film structure + the SFW LoRA resolver; no restricted
import, model, or feature.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import taxonomy, workflow

__all__ = [
    "FilmTemplate",
    "KeyframeSpec",
    "FILM_TEMPLATES",
    "KenBurns",
    "get_template",
    "resolve_film_lora",
    "build_storyboard",
    "FilmTemplateError",
]


class FilmTemplateError(ValueError):
    """An unknown videoType, or a film could not be templated."""


# ── Ken-Burns motion presets (consumed by the mux, lb-4vw) ──────────────────
#
# A pan/zoom over a still keyframe. ``zoom_from``/``zoom_to`` are scale factors
# (1.0 = no zoom); ``pan`` is the direction of drift. The mux (lb-4vw) turns
# these into the ffmpeg zoompan expression; defined here so the storyboard is a
# complete, self-describing render spec.
@dataclass(frozen=True)
class KenBurns:
    """A Ken-Burns pan/zoom preset for one keyframe."""

    zoom_from: float
    zoom_to: float
    pan: str  # "in" | "out" | "left" | "right" | "up" | "down" | "none"

    def as_dict(self) -> dict:
        return {"zoom_from": self.zoom_from, "zoom_to": self.zoom_to, "pan": self.pan}


# Named motion presets the templates draw from.
_STATIC = KenBurns(1.0, 1.0, "none")
_SLOW_IN = KenBurns(1.0, 1.12, "in")
_SLOW_OUT = KenBurns(1.12, 1.0, "out")
_PAN_RIGHT = KenBurns(1.06, 1.06, "right")
_PAN_LEFT = KenBurns(1.06, 1.06, "left")
_DRAMATIC_IN = KenBurns(1.0, 1.25, "in")


@dataclass(frozen=True)
class KeyframeSpec:
    """One keyframe (shot) in a storyboard before its prompt/image are filled.

    ``index`` is the shot order. ``image_type`` is what the frame is rendered as
    (drives the LoRA). ``duration_s`` is how long the keyframe is held on screen
    (the narration for this shot rides over it). ``ken_burns`` is the motion.
    ``prompt_hint`` is the teacher-facing role of the shot ("title card",
    "diagram", …) used as a prompt scaffold by the pipeline; it is NOT a LoRA.
    """

    index: int
    image_type: str
    duration_s: float
    ken_burns: KenBurns
    prompt_hint: str

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "image_type": self.image_type,
            "duration_s": self.duration_s,
            "ken_burns": self.ken_burns.as_dict(),
            "prompt_hint": self.prompt_hint,
        }


@dataclass(frozen=True)
class FilmTemplate:
    """The template for one videoType: how its keyframe storyboard is shaped.

    ``image_type`` is the default image type for the film's keyframes — the
    field that resolves the LoRA. ``shots`` is the ordered keyframe skeleton.
    ``narration_voice`` is the default narration voice slug. ``target_s`` is the
    nominal total length (advisory; the real length follows the narration).
    """

    video_type: str
    name: str
    image_type: str
    narration_voice: str
    target_s: int
    shots: tuple[KeyframeSpec, ...]

    @property
    def shot_count(self) -> int:
        return len(self.shots)

    def as_dict(self) -> dict:
        return {
            "video_type": self.video_type,
            "name": self.name,
            "image_type": self.image_type,
            "narration_voice": self.narration_voice,
            "target_s": self.target_s,
            "shot_count": self.shot_count,
            "shots": [s.as_dict() for s in self.shots],
        }


def _shots(image_type: str, plan: list[tuple[float, KenBurns, str]]) -> tuple[KeyframeSpec, ...]:
    """Build an ordered KeyframeSpec tuple from (duration, motion, hint) rows."""
    return tuple(
        KeyframeSpec(i, image_type, dur, kb, hint)
        for i, (dur, kb, hint) in enumerate(plan)
    )


# ── the 6 film templates (verbatim with the taxonomy videoTypes) ────────────
#
# image_type per film is chosen to match the videoType's pedagogical intent and
# to resolve a sensible LoRA via resolve_lora:
#   lektionsfilm    → illustration  (a held lesson, illustrated keyframes)
#   kort-forklaring → illustration  (a quick illustrated concept beat)
#   animerat-diagram→ diagram       (force vectors / current / processes)
#   tidslinje-anim  → tidslinje     (an animated chronology)
#   ken-burns       → foto          (a photo panned with voice-over)
#   reenactment     → foto          (a dramatised photographic scene)
# Every one of these image types has a base LoRA in the taxonomy, so a film of
# any subject resolves (falling back to the image type's base LoRA when there is
# no subject-specific matrix cell).
FILM_TEMPLATES: dict[str, FilmTemplate] = {
    "lektionsfilm": FilmTemplate(
        "lektionsfilm", "Lektionsfilm", image_type="illustration",
        narration_voice="sv-standard", target_s=120,
        shots=_shots("illustration", [
            (6.0, _SLOW_IN, "titelkort: ämnet introduceras"),
            (12.0, _PAN_RIGHT, "begrepp 1 illustrerat"),
            (12.0, _PAN_LEFT, "begrepp 2 illustrerat"),
            (12.0, _SLOW_IN, "exempel / tillämpning"),
            (10.0, _SLOW_OUT, "sammanfattning"),
        ]),
    ),
    "kort-forklaring": FilmTemplate(
        "kort-forklaring", "Kort förklaring", image_type="illustration",
        narration_voice="sv-standard", target_s=20,
        shots=_shots("illustration", [
            (4.0, _SLOW_IN, "begreppet visas"),
            (8.0, _PAN_RIGHT, "förklaring i en bild"),
            (4.0, _SLOW_OUT, "slutsats"),
        ]),
    ),
    "animerat-diagram": FilmTemplate(
        "animerat-diagram", "Animerat diagram", image_type="diagram",
        narration_voice="sv-standard", target_s=45,
        shots=_shots("diagram", [
            (6.0, _STATIC, "diagrammets utgångsläge"),
            (10.0, _SLOW_IN, "storheter / vektorer markeras"),
            (10.0, _PAN_RIGHT, "processen i rörelse"),
            (8.0, _SLOW_OUT, "resultat / jämvikt"),
        ]),
    ),
    "tidslinje-anim": FilmTemplate(
        "tidslinje-anim", "Tidslinje-animation", image_type="tidslinje",
        narration_voice="sv-standard", target_s=60,
        shots=_shots("tidslinje", [
            (6.0, _STATIC, "tidslinjens början"),
            (10.0, _PAN_RIGHT, "första händelsen"),
            (10.0, _PAN_RIGHT, "andra händelsen"),
            (10.0, _PAN_RIGHT, "tredje händelsen"),
            (8.0, _SLOW_OUT, "nutid / sammanfattning"),
        ]),
    ),
    "ken-burns": FilmTemplate(
        "ken-burns", "Stillbild + voiceover", image_type="foto",
        narration_voice="sv-standard", target_s=40,
        shots=_shots("foto", [
            (10.0, _SLOW_IN, "etablerande stillbild"),
            (12.0, _PAN_RIGHT, "detalj i bilden"),
            (10.0, _SLOW_OUT, "helhet / kontext"),
        ]),
    ),
    "reenactment": FilmTemplate(
        "reenactment", "Historisk reenactment", image_type="foto",
        narration_voice="sv-standard", target_s=60,
        shots=_shots("foto", [
            (8.0, _DRAMATIC_IN, "scenen etableras"),
            (12.0, _PAN_LEFT, "händelsen utspelar sig"),
            (12.0, _PAN_RIGHT, "vändpunkten"),
            (10.0, _SLOW_OUT, "efterspel / betydelse"),
        ]),
    ),
}


def get_template(video_type: str) -> FilmTemplate:
    """The :class:`FilmTemplate` for a videoType slug, or raise.

    Validates against the taxonomy SoT first (so the error lists valid slugs),
    then returns the backend-defined template. Raises :class:`FilmTemplateError`
    for an unknown / missing videoType.
    """
    if not video_type or not taxonomy.is_valid_video_type(video_type):
        raise FilmTemplateError(
            f"unknown videoType {video_type!r}; valid: "
            f"{', '.join(taxonomy.video_type_slugs())}"
        )
    tmpl = FILM_TEMPLATES.get(video_type)
    if tmpl is None:  # pragma: no cover - guarded by the taxonomy check + the
        # all-videoTypes-covered test below; defensive against a future slug add.
        raise FilmTemplateError(f"no film template defined for videoType {video_type!r}")
    return tmpl


def resolve_film_lora(video_type: str, subject: str) -> dict:
    """The backend-authoritative, allowlisted LoRA for a film's keyframes.

    Maps ``videoType → imageType`` via the template, then resolves the LoRA for
    ``(subject, imageType)`` through :func:`app.workflow.resolve_and_validate_lora`
    — the SAME choke point the stills path uses, so the SFW allowlist is enforced
    identically (a film can never name a LoRA, and an off-allowlist resolution
    fails closed). Returns the resolved LoRA dict (``{name, strength, source,
    file, …}``). Raises :class:`FilmTemplateError` for an unknown videoType and
    propagates :class:`app.workflow.WorkflowError` for a bad subject.
    """
    tmpl = get_template(video_type)
    return workflow.resolve_and_validate_lora(subject, tmpl.image_type)


def build_storyboard(
    video_type: str,
    subject: str,
    area: str,
    *,
    narration_voice: Optional[str] = None,
) -> dict:
    """A render-ready storyboard skeleton for ``(videoType, subject, area)``.

    Combines the template's keyframe skeleton with the resolved LoRA + the
    taxonomy keys, producing the spec the pipeline (lb-4vw) fills with prompts +
    renders. Validates the subject/area pair against the taxonomy SoT. The
    keyframes all carry the template's image type (so each resolves the same
    allowlisted LoRA at render time). ``narration_voice`` overrides the
    template default (validated against the taxonomy narrationVoices).

    Returns a plain dict (JSON-serialisable). Raises :class:`FilmTemplateError`
    for an unknown videoType / invalid subject-area / invalid voice.
    """
    tmpl = get_template(video_type)
    if not taxonomy.is_valid_subject(subject):
        raise FilmTemplateError(f"unknown subject {subject!r}")
    if not taxonomy.is_valid_area(subject, area):
        raise FilmTemplateError(
            f"unknown area {area!r} for subject {subject!r}; valid: "
            f"{', '.join(taxonomy.area_slugs(subject))}"
        )

    voice = narration_voice or tmpl.narration_voice
    valid_voices = {v["slug"] for v in taxonomy.NARRATION_VOICES}
    if voice not in valid_voices:
        raise FilmTemplateError(
            f"unknown narration voice {voice!r}; valid: {', '.join(sorted(valid_voices))}"
        )

    lora = resolve_film_lora(video_type, subject)
    return {
        "video_type": tmpl.video_type,
        "subject": subject,
        "area": area,
        "image_type": tmpl.image_type,
        "narration_voice": voice,
        "target_s": tmpl.target_s,
        "lora": lora,
        "shots": [s.as_dict() for s in tmpl.shots],
    }
