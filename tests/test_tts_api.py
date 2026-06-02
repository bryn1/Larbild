"""Route tests for POST /api/tts + GET /api/tts/voices (lb-ayi, P6.1).

The route is tested with ``app.tts.synthesize`` monkeypatched, so no network /
VM600 call is made — the HTTP contract (status codes, content-type, error
mapping) is what's under test here; the synthesis itself is covered by
test_tts.py.
"""
from __future__ import annotations

import pytest

from app import create_app
from app import tts as tts_mod


@pytest.fixture
def client():
    app = create_app({"TESTING": True, "INIT_DB": False})
    return app.test_client()


def test_voices_lists_two_narration_voices(client):
    r = client.get("/api/tts/voices")
    assert r.status_code == 200
    slugs = {v["slug"] for v in r.get_json()["voices"]}
    assert slugs == {"sv-standard", "sv-lattlast"}


def test_post_tts_returns_wav(client, monkeypatch):
    monkeypatch.setattr(tts_mod, "synthesize", lambda text, voice: b"RIFFmockwav")
    r = client.post("/api/tts", json={"text": "Hej världen", "voice": "sv-standard"})
    assert r.status_code == 200
    assert r.mimetype == "audio/wav"
    assert r.data == b"RIFFmockwav"


def test_post_tts_defaults_to_standard_voice(client, monkeypatch):
    seen = {}

    def _synth(text, voice):
        seen["voice"] = voice
        return b"wav"
    monkeypatch.setattr(tts_mod, "synthesize", _synth)
    r = client.post("/api/tts", json={"text": "Hej"})
    assert r.status_code == 200
    assert seen["voice"] == "sv-standard"


def test_post_tts_empty_text_400(client):
    r = client.post("/api/tts", json={"text": "  "})
    assert r.status_code == 400


def test_post_tts_unknown_voice_400(client):
    r = client.post("/api/tts", json={"text": "hej", "voice": "de-DE"})
    assert r.status_code == 400
    assert "valid_voices" in r.get_json()


def test_post_tts_service_down_maps_502(client, monkeypatch):
    def _down(text, voice):
        raise tts_mod.TTSUnavailable("voice_service unreachable")
    monkeypatch.setattr(tts_mod, "synthesize", _down)
    r = client.post("/api/tts", json={"text": "hej", "voice": "sv-standard"})
    assert r.status_code == 502
    assert r.get_json()["error"] == "narration voice service unavailable"


def test_post_tts_bad_input_maps_400(client, monkeypatch):
    def _bad(text, voice):
        raise tts_mod.TTSError("narration text too long")
    monkeypatch.setattr(tts_mod, "synthesize", _bad)
    r = client.post("/api/tts", json={"text": "hej", "voice": "sv-standard"})
    assert r.status_code == 400


def test_post_tts_accepts_form_encoding(client, monkeypatch):
    monkeypatch.setattr(tts_mod, "synthesize", lambda text, voice: b"wav")
    r = client.post("/api/tts", data={"text": "Hej", "voice": "sv-lattlast"})
    assert r.status_code == 200
    assert r.mimetype == "audio/wav"
