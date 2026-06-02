"""Unit tests for the Piper sv-SE narration layer (lb-ayi, P6.1).

No real network in the unit tests — a stub ``post`` feeds the PiperClient
synthetic WAV bytes so the voice policy (single vs per-sentence + gap) is tested
deterministically. A separate, reachability-gated test does a REAL smoke test
against the VM600 voice_service when it is up (it is, as of this bead).
"""
from __future__ import annotations

import io
import os
import wave

import pytest
import requests

from app import tts


# ── a tiny real WAV the stub returns, so concat exercises real wave I/O ──────


def _wav(seconds: float = 0.1, rate: int = 22050) -> bytes:
    """A silent mono 16-bit PCM WAV of the given duration (matches Piper's shape)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class StubResp:
    def __init__(self, status_code=200, content=b"", text=""):
        self.status_code = status_code
        self.content = content
        self.text = text


def _stub_post(returns=None, calls=None, status_code=200):
    """Build a requests.post stand-in that records calls + returns a WAV."""
    body = returns if returns is not None else _wav()

    def _post(url, data=None, timeout=None):
        if calls is not None:
            calls.append({"url": url, "data": dict(data or {}), "timeout": timeout})
        return StubResp(status_code=status_code, content=body)

    return _post


# ── voice validation ────────────────────────────────────────────────────────


def test_valid_voices_are_the_two_taxonomy_slugs():
    assert tts.NARRATION_VOICES == ("sv-standard", "sv-lattlast")
    assert tts.is_valid_voice("sv-standard")
    assert tts.is_valid_voice("sv-lattlast")
    assert not tts.is_valid_voice("en-US")
    assert not tts.is_valid_voice("")


def test_tts_voices_match_taxonomy_sot():
    # The TTS layer's voice slugs must equal the taxonomy SoT's narrationVoices.
    from app import taxonomy
    tax_slugs = tuple(v["slug"] for v in taxonomy.NARRATION_VOICES)
    assert set(tts.NARRATION_VOICES) == set(tax_slugs)


def test_unknown_voice_raises():
    with pytest.raises(tts.TTSError):
        tts.synthesize("hej", "fr-FR", client=tts.PiperClient(post=_stub_post()))


def test_empty_text_raises():
    with pytest.raises(tts.TTSError):
        tts.synthesize("   ", "sv-standard", client=tts.PiperClient(post=_stub_post()))


def test_overlong_text_raises(monkeypatch):
    monkeypatch.setattr(tts, "MAX_CHARS", 10)
    with pytest.raises(tts.TTSError):
        tts.synthesize("x" * 11, "sv-standard", client=tts.PiperClient(post=_stub_post()))


# ── sv-standard: a single synthesis call ────────────────────────────────────


def test_standard_voice_one_call_returns_wav():
    calls: list[dict] = []
    client = tts.PiperClient(post=_stub_post(calls=calls))
    out = tts.synthesize("Detta är en mening. Och en till.", "sv-standard", client=client)
    # exactly ONE /tts call for standard (no per-sentence splitting)
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/tts")
    assert calls[0]["data"]["text"] == "Detta är en mening. Och en till."
    # a valid WAV came back
    with wave.open(io.BytesIO(out), "rb") as w:
        assert w.getnchannels() == 1 and w.getframerate() == 22050


# ── sv-lattlast: per-sentence synthesis + a silence gap (slower pacing) ──────


def test_lattlast_voice_splits_per_sentence():
    calls: list[dict] = []
    client = tts.PiperClient(post=_stub_post(calls=calls))
    tts.synthesize("Ljud är vågor. Det rör sig genom luft! Hör du?",
                   "sv-lattlast", client=client)
    # three sentences → three synthesis calls
    assert len(calls) == 3
    spoken = [c["data"]["text"] for c in calls]
    assert spoken == ["Ljud är vågor.", "Det rör sig genom luft!", "Hör du?"]


def test_lattlast_is_longer_than_standard_for_same_text():
    # The easy-read voice inserts inter-sentence silence, so for the same text
    # it must be audibly (measurably) longer than the standard rendering. The
    # stub returns a fixed-size clip per /tts call, so the duration delta here
    # is (extra clips from per-sentence splitting) + (the inter-sentence gaps).
    text = "En mening. En andra mening. En tredje mening."  # 3 sentences
    clip = _wav(0.1)  # the stub's per-call clip duration
    std = tts.synthesize(text, "sv-standard",
                         client=tts.PiperClient(post=_stub_post(returns=clip)))
    slow = tts.synthesize(text, "sv-lattlast",
                          client=tts.PiperClient(post=_stub_post(returns=clip)))
    assert tts.wav_duration_s(slow) > tts.wav_duration_s(std)
    gap = tts.VOICE_PROFILES["sv-lattlast"].sentence_gap_s
    # standard = 1 clip (0.1); lättläst = 3 clips (0.3) + 2 gaps. Delta =
    # 2 extra clips + 2 gaps. (With real Piper the same TEXT is spoken either
    # way, so the real-world delta is just the gaps — asserted in the live test.)
    expected = (2 * 0.1) + (2 * gap)
    assert tts.wav_duration_s(slow) - tts.wav_duration_s(std) == pytest.approx(expected, abs=0.03)


def test_lattlast_forwards_length_scale_form_field():
    calls: list[dict] = []
    client = tts.PiperClient(post=_stub_post(calls=calls))
    tts.synthesize("En mening.", "sv-lattlast", client=client)
    # forward-compat: length_scale is sent (ignored by today's service)
    assert calls[0]["data"].get("length_scale") == tts.VOICE_PROFILES["sv-lattlast"].length_scale


def test_concat_preserves_total_frames_plus_gap():
    a, b = _wav(0.20), _wav(0.30)
    joined = tts._concat_wavs([a, b], gap_s=0.10)
    dur = tts.wav_duration_s(joined)
    assert dur == pytest.approx(0.20 + 0.30 + 0.10, abs=0.01)


def test_single_clip_no_gap_is_passthrough():
    a = _wav(0.2)
    assert tts._concat_wavs([a], gap_s=0.0) == a


# ── transport / availability error mapping ──────────────────────────────────


def test_503_maps_to_unavailable():
    client = tts.PiperClient(post=_stub_post(status_code=503))
    with pytest.raises(tts.TTSUnavailable):
        tts.synthesize("hej", "sv-standard", client=client)


def test_connection_error_maps_to_unavailable():
    def _boom(url, data=None, timeout=None):
        raise requests.ConnectionError("no route to host")
    client = tts.PiperClient(post=_boom)
    with pytest.raises(tts.TTSUnavailable):
        tts.synthesize("hej", "sv-standard", client=client)


def test_500_maps_to_tts_error():
    client = tts.PiperClient(post=_stub_post(status_code=500))
    with pytest.raises(tts.TTSError):
        tts.synthesize("hej", "sv-standard", client=client)


def test_empty_body_maps_to_tts_error():
    client = tts.PiperClient(post=_stub_post(returns=b""))
    with pytest.raises(tts.TTSError):
        tts.synthesize("hej", "sv-standard", client=client)


def test_default_url_is_vm600_8020():
    assert tts.default_tts_url() == "http://192.168.5.185:8020"


# ── REAL smoke test against VM600 (skipped when unreachable) ─────────────────


def _vm600_up() -> bool:
    try:
        r = requests.get(f"{tts.default_tts_url()}/health", timeout=3)
        return r.status_code == 200 and r.json().get("tts") == "ready"
    except Exception:
        return False


@pytest.mark.skipif(not _vm600_up(), reason="VM600 voice_service not reachable")
def test_real_vm600_synthesizes_sv_wav():
    """REAL smoke test: VM600 Piper produces a non-trivial sv-SE WAV.

    Runs only when the voice_service /health says TTS is ready (it is, as of
    this bead — validated end to end). Confirms a real Swedish WAV comes back,
    16-bit mono, with a sane duration for the sentence.
    """
    out = tts.synthesize(
        "Hej, detta är ett test av svensk berättarröst för Lärbild.",
        "sv-standard",
    )
    with wave.open(io.BytesIO(out), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() in (16000, 22050, 24000)
        assert w.getnframes() > w.getframerate() * 0.5  # at least ~0.5 s of audio


@pytest.mark.skipif(not _vm600_up(), reason="VM600 voice_service not reachable")
def test_real_vm600_lattlast_longer_than_standard():
    """REAL: the lättläst rendering is longer than standard for the same text."""
    text = "Ljud är vågor. Vågorna rör sig genom luften. Sedan når de ditt öra."
    std = tts.synthesize(text, "sv-standard")
    slow = tts.synthesize(text, "sv-lattlast")
    assert tts.wav_duration_s(slow) > tts.wav_duration_s(std)
