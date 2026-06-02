"""Narration TTS endpoint (lb-ayi, P6.1).

Exposes the Swedish Piper narration layer (app.tts → VM600 voice_service) over
HTTP so the Skapa-film UI can preview a voice and the film pipeline has a stable
contract:

  * ``POST /api/tts``        — ``{text, voice}`` → an ``audio/wav`` body. ``voice``
    is one of the two narration slugs (``sv-standard`` / ``sv-lattlast``); it
    defaults to ``sv-standard``. 400 on a bad voice / empty text, 502/503 when
    the VM600 voice_service is unreachable or has TTS disabled.
  * ``GET  /api/tts/voices`` — the available narration voices (slug + label +
    description), sourced from the taxonomy SoT, for the voice picker.

The audio is the same PCM WAV the ffmpeg mux step consumes, so a preview and the
real film narration are byte-for-byte the same path.

SFW-only: narration text → speech + a static voice list; no restricted import,
model, or feature.
"""
from __future__ import annotations

from flask import Blueprint, Response, jsonify, request

from .. import taxonomy, tts

tts_bp = Blueprint("tts", __name__)


def _body() -> dict:
    if request.is_json:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            return data
    if request.form:
        return dict(request.form)
    return {}


@tts_bp.get("/api/tts/voices")
def list_voices():
    """The narration voices (taxonomy SoT) for the voice picker."""
    return jsonify({"voices": [dict(v) for v in taxonomy.NARRATION_VOICES]}), 200


@tts_bp.post("/api/tts")
def synthesize():
    """Synthesise narration → WAV. ``{text, voice}`` in; ``audio/wav`` out.

    Returns 400 for an empty text or an unknown voice (the TTS layer validates
    both), 502 when the voice_service is unreachable / TTS is down, and the WAV
    body with ``Content-Type: audio/wav`` on success.
    """
    data = _body()
    text = (data.get("text") or "").strip()
    voice = (data.get("voice") or "sv-standard").strip()

    if not text:
        return jsonify({"error": "text is required"}), 400
    if not tts.is_valid_voice(voice):
        return jsonify({"error": f"unknown voice: {voice!r}",
                        "valid_voices": list(tts.NARRATION_VOICES)}), 400

    try:
        wav = tts.synthesize(text, voice)
    except tts.TTSUnavailable as e:
        # Infra: VM600 voice_service down / Piper unavailable → 502 (upstream).
        return jsonify({"error": "narration voice service unavailable",
                        "detail": str(e)}), 502
    except tts.TTSError as e:
        # Bad input that slipped past the cheap checks (e.g. over-length).
        return jsonify({"error": str(e)}), 400

    return Response(
        wav,
        mimetype="audio/wav",
        headers={
            "Content-Disposition": f'inline; filename="narration-{voice}.wav"',
            "Cache-Control": "no-store",
        },
    )
