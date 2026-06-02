"""Lesson/short manuscript submission + progress API (lb-lesson).

The Lärbild pedagogical-video funnel — the manuscript-driven lesson/overview
system — built on the SAME jobs/SSE pattern as films + stills:

  * ``POST /api/lessons`` — accept a MANUSCRIPT (the Opus-generated pedagogical
    script: a delområde + level + ordered begrepp beats; the documented schema
    lives in :mod:`app.lesson`), validate it against the taxonomy SoT + the lesson
    model, enqueue it, and return ``202 {job_id, status:"queued", kind}``. The
    render assembles each beat (keyframe → bildtext overlay → narration), merges
    the beats into the overview video, and auto-sorts it into the ``library`` as a
    level-tagged VIDEO record — flowing into the existing review/regenerate loop.
    With ``allLevels:true`` (overview only) the render produces all three level-
    versions from one keyframe render.
  * ``GET  /api/lessons/schema`` — the manuscript JSON schema (so the Opus step,
    or a UI, knows exactly what to produce) + the levels/voices/motions enums.
  * ``POST /api/lessons/preview`` — validate a manuscript + return its parsed
    facets + an ESTIMATED merged duration (no render) — a dry-run for the author.
  * ``GET  /api/lessons/<id>``        — the current lesson job state.
  * ``GET  /api/lessons/<id>/events`` — SSE progress. A lesson is a ``jobs`` row,
    so this delegates to :func:`app.routes.jobs.job_events` (RESUME on connect,
    one event per state change, terminal close). No duplicate SSE machinery.

The LoRA is NEVER accepted from a manuscript (backend-authoritative, resolved per
keyframe from subject + imageType — the SFW guard); any ``lora`` field is ignored.

SFW-only: manuscript validation + queue plumbing + schema listing; no restricted
import, model, content, or reference.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from .. import assembly, lesson as lesson_mod, taxonomy, tts
from ..jobs import QueueFull, read_job_state
from .jobs import job_events as _job_events

lessons_bp = Blueprint("lessons", __name__)


def _body() -> dict:
    if request.is_json:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            return data
    if request.form:
        return dict(request.form)
    return {}


def _validate_manuscript(data: dict):
    """Validate a manuscript against the taxonomy SoT + the lesson model.

    Two-stage, so the error is specific:
      1. taxonomy gate — subject/area must be valid LGR22 slugs (the SoT, lb-582);
         level (if present) must be one of the three school levels; the narration
         voice (if present) must be a known voice. This is the layer the lesson
         model deliberately does NOT do (it stays taxonomy-free + unit-testable).
      2. shape gate — :func:`app.lesson.Lesson.parse` validates the beats +
         captions + motions + kind.

    Returns ``(lesson, all_levels, None)`` on success or ``(None, None, (payload,
    code))`` on the first failure.
    """
    if not isinstance(data, dict) or not data:
        return None, None, ({"error": "a manuscript JSON object is required",
                             "schema": "/api/lessons/schema"}, 400)

    subject = str(data.get("subject") or "").strip()
    area = str(data.get("area") or "").strip()
    if not subject or not area:
        return None, None, ({"error": "manuscript needs 'subject' and 'area'",
                             "valid_subjects": list(taxonomy.subject_slugs())}, 400)
    if not taxonomy.is_valid_subject(subject):
        return None, None, ({"error": f"unknown subject: {subject!r}",
                             "valid_subjects": list(taxonomy.subject_slugs())}, 400)
    if not taxonomy.is_valid_area(subject, area):
        return None, None, ({"error": f"unknown area {area!r} for subject {subject!r}",
                             "valid_areas": list(taxonomy.area_slugs(subject))}, 400)

    # level: optional. When present it must be one of the three school levels
    # (accepting the å spelling, normalised by the model). A family short may omit
    # it (it compares concepts rather than scaling one explanation across levels).
    raw_level = data.get("level")
    if raw_level not in (None, ""):
        norm = str(raw_level).strip().lower().replace("ä", "a").replace("å", "a").replace("ö", "o")
        if norm not in lesson_mod.LEVELS:
            return None, None, ({"error": f"unknown level: {raw_level!r}",
                                 "valid_levels": list(lesson_mod.LEVELS)}, 400)

    voice = data.get("narration_voice") or data.get("narrationVoice")
    if voice not in (None, "") and not tts.is_valid_voice(str(voice).strip()):
        return None, None, ({"error": f"unknown narrationVoice: {voice!r}",
                             "valid_voices": list(tts.NARRATION_VOICES)}, 400)

    # allLevels: produce all three level-versions (overview only). Accept the
    # camelCase + snake_case spellings + truthy strings.
    all_levels = _truthy(data.get("allLevels", data.get("all_levels", False)))

    # The manuscript carries the LoRA backend-side; strip any client-supplied one.
    clean = {k: v for k, v in data.items() if k not in ("lora", "allLevels", "all_levels")}
    # normalise the voice alias the model reads.
    if "narrationVoice" in clean and "narration_voice" not in clean:
        clean["narration_voice"] = clean.pop("narrationVoice")

    try:
        lesson = lesson_mod.Lesson.parse(clean)
    except lesson_mod.LessonError as e:
        return None, None, ({"error": str(e), "schema": "/api/lessons/schema"}, 400)

    if all_levels and lesson.kind != "lesson":
        return None, None, ({"error": "allLevels is only valid for a kind='lesson' overview"}, 400)

    return lesson, all_levels, None


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return False


# ── manuscript schema (the contract the Opus step produces) ──────────────────

# The documented manuscript JSON schema, served so the Opus author (or a UI) knows
# exactly what to emit. Mirrors the docstring in app.lesson — kept here as a
# machine-readable shape (a light JSON-Schema-ish description, not a validator;
# the real validation is Lesson.parse + the taxonomy gate).
_MANUSCRIPT_SCHEMA = {
    "title": "Lärbild lesson manuscript",
    "description": (
        "A per-delområde OVERVIEW video manuscript (kind='lesson') walking ALL "
        "begrepp in a sub-area at one school level, OR a per-begrepp / family "
        "short (kind='short'). The Opus step (offline, skola-begrepp pattern) "
        "produces this; the assembly consumes it."
    ),
    "type": "object",
    "required": ["subject", "area", "beats"],
    "properties": {
        "subject": {"type": "string", "description": "LGR22 subject slug (taxonomy SoT)"},
        "area": {"type": "string", "description": "LGR22 area slug (the delområde), scoped to subject"},
        "level": {"type": ["string", "null"], "enum": list(lesson_mod.LEVELS) + [None],
                  "description": "school level; null for a family short that compares concepts"},
        "kind": {"type": "string", "enum": ["lesson", "short"], "default": "lesson"},
        "title": {"type": ["string", "null"]},
        "narration_voice": {"type": "string", "enum": list(tts.NARRATION_VOICES),
                            "default": "sv-standard"},
        "duration_target_s": {"type": ["integer", "null"],
                              "description": "advisory target length (5-10 min for an overview)"},
        "beats": {
            "type": "array", "minItems": 1,
            "description": "ordered beats, one per begrepp in the delområde",
            "items": {
                "type": "object",
                "required": ["begrepp"],
                "properties": {
                    "begrepp": {"type": "string", "description": "the concept name (required)"},
                    "forklaring": {"type": "string", "description": "the Förklaring (skola shape)"},
                    "vardagligt_exempel": {"type": "string", "description": "the Vardagligt exempel"},
                    "image_prompt": {"type": "string", "description": "what the keyframe shows (no LoRA)"},
                    "caption": {
                        "description": "the bildtext: a flat string OR {short,longer,long} (=enkel/utvecklad/valutvecklad)",
                        "oneOf": [
                            {"type": "string"},
                            {"type": "object", "properties": {
                                "short": {"type": "string"}, "longer": {"type": "string"},
                                "long": {"type": "string"}}},
                        ],
                    },
                    "narration": {"type": "string", "description": "the spoken talking-point line"},
                    "connection": {"type": "string", "description": "link to the NEXT beat (spoken as the narration tail)"},
                    "motion": {"type": "string", "enum": list(lesson_mod.MOTIONS), "default": "still"},
                    "image_type": {"type": "string", "default": "illustration",
                                  "description": "imageTypes slug (drives the SFW LoRA)"},
                    "duration_s": {"type": ["number", "null"],
                                  "description": "explicit hold (else derived from the narration length)"},
                },
            },
        },
    },
    "submission": {
        "POST /api/lessons": "the manuscript above, plus optional allLevels:true "
                             "(overview only) to render all three level-versions",
    },
}


@lessons_bp.get("/api/lessons/schema")
def get_schema():
    """The manuscript JSON schema + the levels / voices / motions enums.

    What the Opus step (or a manuscript-authoring UI) needs to produce a valid
    manuscript. Read-only.
    """
    return jsonify({
        "schema": _MANUSCRIPT_SCHEMA,
        "levels": list(lesson_mod.LEVELS),
        "caption_rungs": list(lesson_mod.CAPTION_RUNGS),
        "level_to_caption_rung": dict(lesson_mod.LEVEL_TO_CAPTION_RUNG),
        "motions": list(lesson_mod.MOTIONS),
        "narration_voices": list(tts.NARRATION_VOICES),
        "kinds": ["lesson", "short"],
    }), 200


@lessons_bp.post("/api/lessons/preview")
def preview_lesson():
    """Validate a manuscript + return its parsed facets + an estimated duration.

    A dry-run for the manuscript author: no render is queued. Returns the parsed
    subject/area/level/kind + begrepp_count + the per-beat captions/narration as
    the assembly would read them, plus an ESTIMATED merged length (words-per-
    minute, advisory). 400 on a bad manuscript (same gate as the real submit).
    """
    lesson, all_levels, err = _validate_manuscript(_body())
    if err is not None:
        payload, status = err
        return jsonify(payload), status
    est = assembly.estimate_lesson_duration_s(lesson)
    return jsonify({
        "ok": True,
        "kind": lesson.kind,
        "subject": lesson.subject,
        "area": lesson.area,
        "level": lesson.level,
        "all_levels": all_levels,
        "title": lesson.display_title(),
        "begrepp_count": lesson.begrepp_count,
        "narration_voice": lesson.narration_voice,
        "duration_target_s": lesson.duration_target_s,
        "estimated_duration_s": round(est, 1),
        "beats": [
            {
                "index": b.index,
                "begrepp": b.begrepp,
                "caption": b.caption_for(lesson.level),
                "narration": b.narration_for(lesson.level),
                "motion": b.motion,
                "estimated_duration_s": round(
                    assembly.estimate_beat_duration_s(b, lesson.level), 1),
            }
            for b in lesson.beats
        ],
    }), 200


@lessons_bp.post("/api/lessons")
def submit_lesson():
    """Validate + enqueue a manuscript. Returns 202 {job_id, status, kind}.

    202 (Accepted) — the lesson/short is queued (it fans out into per-beat
    keyframe renders + caption overlays + narration + a merge); the client then
    tails ``/api/lessons/<id>/events``. 400 on a bad manuscript; 429 when the
    render queue is full.
    """
    lesson, all_levels, err = _validate_manuscript(_body())
    if err is not None:
        payload, status = err
        return jsonify(payload), status
    try:
        job_id = assembly.enqueue_lesson(lesson.as_dict(), all_levels=all_levels)
    except assembly.AssemblyError as e:
        return jsonify({"error": str(e)}), 400
    except QueueFull as e:
        return jsonify({"error": "render queue is full, try again shortly",
                        "depth": e.depth, "limit": e.limit}), 429
    return jsonify({"job_id": job_id, "status": "queued", "kind": lesson.kind,
                    "all_levels": all_levels}), 202


@lessons_bp.get("/api/lessons/<job_id>")
def get_lesson(job_id: str):
    """The current state of a lesson job, or 404 if unknown."""
    state = read_job_state(job_id)
    if state is None:
        return jsonify({"error": "unknown lesson", "job_id": job_id}), 404
    return jsonify(state), 200


@lessons_bp.get("/api/lessons/<job_id>/events")
def lesson_events(job_id: str):
    """SSE progress for a lesson — delegates to the shared jobs SSE stream.

    A lesson is a ``jobs`` row, so its progress streams through exactly the same
    machinery as a still/film job (RESUME on connect, one event per change,
    terminal close, the same connection cap + idle timeout). No duplicate SSE code.
    """
    return _job_events(job_id)
