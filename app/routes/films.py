"""Film submission + progress API (lb-4vw, P6.3).

The film funnel's contract, built on the same jobs/SSE pattern as stills:

  * ``POST /api/films``            — validate a film spec (videoType + subject/area
    + narration script, optional voice + per-shot prompts) against the taxonomy
    SoT + film templates, enqueue it, return ``202 {job_id, status: "queued"}``.
  * ``GET  /api/films/templates``  — the 6 film templates (shot skeleton, image
    type, default voice) for the Skapa-film UI.
  * ``GET  /api/films/<id>``       — the current film job state.
  * ``GET  /api/films/<id>/events`` — SSE progress. A film is a ``jobs`` row
    (``kind='film'``), so this is the SAME stream the still jobs use — it
    delegates to :func:`app.routes.jobs.job_events` (RESUME on connect, one event
    per state change, terminal close). No duplicate SSE machinery.

The LoRA is never accepted from the client (backend-authoritative, resolved per
keyframe from subject + imageType — the SFW guard); a ``lora`` field is ignored.

SFW-only: spec validation + queue plumbing + template listing; no restricted
import, model, or feature.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from .. import film, film_templates, taxonomy, tts
from ..jobs import QueueFull, read_job_state
from .jobs import job_events as _job_events

films_bp = Blueprint("films", __name__)


def _body() -> dict:
    if request.is_json:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            return data
    if request.form:
        return dict(request.form)
    return {}


def _validate(data: dict):
    """Validate + normalise a film spec. Returns (clean, None) or (None, (err, code))."""
    video_type = (data.get("videoType") or data.get("video_type") or "").strip()
    if not video_type:
        return None, ({"error": "videoType is required",
                       "valid_video_types": list(taxonomy.video_type_slugs())}, 400)
    if not taxonomy.is_valid_video_type(video_type):
        return None, ({"error": f"unknown videoType: {video_type!r}",
                       "valid_video_types": list(taxonomy.video_type_slugs())}, 400)

    subject = (data.get("subject") or "").strip()
    area = (data.get("area") or "").strip()
    if not subject or not area:
        return None, ({"error": "subject and area are required",
                       "valid_subjects": list(taxonomy.subject_slugs())}, 400)
    if not taxonomy.is_valid_subject(subject):
        return None, ({"error": f"unknown subject: {subject!r}",
                       "valid_subjects": list(taxonomy.subject_slugs())}, 400)
    if not taxonomy.is_valid_area(subject, area):
        return None, ({"error": f"unknown area {area!r} for subject {subject!r}",
                       "valid_areas": list(taxonomy.area_slugs(subject))}, 400)

    narration = (data.get("narration") or "").strip()
    if not narration:
        return None, ({"error": "narration script is required"}, 400)

    voice = (data.get("narrationVoice") or data.get("narration_voice") or "").strip() or None
    if voice is not None and not tts.is_valid_voice(voice):
        return None, ({"error": f"unknown narrationVoice: {voice!r}",
                       "valid_voices": list(tts.NARRATION_VOICES)}, 400)

    shot_prompts = data.get("shotPrompts") or data.get("shot_prompts")
    if shot_prompts is not None:
        if not isinstance(shot_prompts, list) or not all(isinstance(s, str) for s in shot_prompts):
            return None, ({"error": "shotPrompts must be a list of strings"}, 400)

    return {
        "video_type": video_type,
        "subject": subject,
        "area": area,
        "narration": narration,
        "title": (data.get("title") or "").strip() or None,
        "narration_voice": voice,
        "shot_prompts": shot_prompts,
    }, None


@films_bp.get("/api/films/templates")
def list_templates():
    """The 6 film templates (skeleton + image type + default voice) for the UI."""
    return jsonify({
        "templates": [t.as_dict() for t in film_templates.FILM_TEMPLATES.values()]
    }), 200


@films_bp.post("/api/films")
def submit_film():
    """Validate + enqueue a film. Returns 202 {job_id, status}.

    202 (Accepted) — the film is queued (it fans out into keyframe renders +
    narration + a mux); the client then tails ``/api/films/<id>/events``. 400 on
    a bad spec; 429 when the queue is full.
    """
    clean, err = _validate(_body())
    if err is not None:
        payload, status = err
        return jsonify(payload), status
    try:
        job_id = film.enqueue_film(**clean)
    except film.FilmError as e:
        return jsonify({"error": str(e)}), 400
    except QueueFull as e:
        return jsonify({"error": "render queue is full, try again shortly",
                        "depth": e.depth, "limit": e.limit}), 429
    return jsonify({"job_id": job_id, "status": "queued", "kind": "film"}), 202


@films_bp.get("/api/films/<job_id>")
def get_film(job_id: str):
    """The current state of a film job, or 404 if unknown."""
    state = read_job_state(job_id)
    if state is None:
        return jsonify({"error": "unknown film", "job_id": job_id}), 404
    return jsonify(state), 200


@films_bp.get("/api/films/<job_id>/events")
def film_events(job_id: str):
    """SSE progress for a film — delegates to the shared jobs SSE stream.

    A film is a ``jobs`` row, so its progress streams through exactly the same
    machinery as a still job (RESUME on connect, one event per change, terminal
    close, the same connection cap + idle timeout). No duplicate SSE code.
    """
    return _job_events(job_id)
