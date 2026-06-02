"""Swedish (sv-SE) narration via Piper on VM600 (lb-ayi, P6.1).

Lärbild films carry a Swedish voice-over. The TTS engine is **Piper sv-SE**,
already running on VM600 (``app@192.168.5.185``) inside the Gas Town
``voice_service`` (``POST /tts`` on port 8020, form field ``text`` → a
``audio/wav`` body). This module is the thin client + the narration-voice
policy; it does NOT run a model locally and is GPU-independent (Piper runs on
the VM600 CPU — no contention with the imagegen sweep on VM100/vm153).

Two narration voices, exactly the taxonomy's ``narrationVoices`` (lb-582):

  * ``sv-standard``  — standard Swedish, neutral pace. One synthesis call.
  * ``sv-lattlast``  — *lättläst* / easy-read for åk 7 / SVA: a **slower,
    clearer** delivery. The shared VM600 voice_service exposes a single Piper
    voice with no speed/length knob (``synthesize_wav(text)`` only), so the
    slow-down is produced **client-side and deterministically**: the text is
    split into sentences, each is synthesised on its own, and the clips are
    concatenated with a short silence between them. That lengthens inter-
    sentence pauses (the dominant cue of "easy-read" pacing) without needing a
    Piper parameter the remote service doesn't offer. The per-voice tuning
    (gap length) is in :data:`VOICE_PROFILES`; if VM600 later exposes a
    ``length_scale`` it can be added there without changing callers.

The returned bytes are a standard PCM **WAV** (16-bit mono, the rate Piper
emits — 22050 Hz for sv_SE-nst-medium). That is exactly what the ffmpeg mux
step (lb-4vw) consumes as the film's audio track.

SFW-only: this is narration text → speech for educational films. It carries no
model name beyond the Swedish Piper voice, no restricted import, feature, or
reference. The text it speaks is the teacher's lesson narration.

Env overrides:
  LARBILD_TTS_URL            default http://192.168.5.185:8020 (VM600 voice_service)
  LARBILD_TTS_TIMEOUT_S      default 120.0  (whole-narration synthesis budget)
  LARBILD_TTS_MAX_CHARS      default 5000   (matches the voice_service /tts cap)
"""
from __future__ import annotations

import io
import os
import re
import wave
from dataclasses import dataclass
from typing import Callable, Optional

import requests

__all__ = [
    "TTSError",
    "TTSUnavailable",
    "NARRATION_VOICES",
    "VOICE_PROFILES",
    "is_valid_voice",
    "default_tts_url",
    "synthesize",
    "PiperClient",
]

# The two narration voices Lärbild ships — the taxonomy's narrationVoices slugs
# (kept here too so the TTS layer validates without importing the whole
# taxonomy; app.taxonomy.NARRATION_VOICES is the SoT and lists the same slugs).
NARRATION_VOICES = ("sv-standard", "sv-lattlast")


@dataclass(frozen=True)
class VoiceProfile:
    """How a narration voice is realised on top of the single VM600 Piper voice.

    ``sentence_gap_s`` is the silence inserted between sentences (the easy-read
    slow-down cue). ``length_scale`` is forwarded to the voice_service as an
    optional form field for forward-compatibility — today's service ignores
    unknown form fields, so a value here is harmless until VM600 honours it.
    """

    slug: str
    label: str
    sentence_gap_s: float
    length_scale: Optional[float] = None


# sv-standard: one synthesis call, natural pace (no inter-sentence padding).
# sv-lattlast: per-sentence synthesis + a generous gap → the slower, clearer
# pacing åk 7 / SVA needs. length_scale is the value VM600 *would* use if it
# exposed Piper's tempo knob (1.0 = normal; >1 = slower).
VOICE_PROFILES: dict[str, VoiceProfile] = {
    "sv-standard": VoiceProfile("sv-standard", "Svenska", sentence_gap_s=0.0,
                                length_scale=1.0),
    "sv-lattlast": VoiceProfile("sv-lattlast", "Lättläst svenska",
                                sentence_gap_s=0.45, length_scale=1.18),
}

DEFAULT_TIMEOUT_S = float(os.environ.get("LARBILD_TTS_TIMEOUT_S", "120.0"))
MAX_CHARS = int(os.environ.get("LARBILD_TTS_MAX_CHARS", "5000"))

# Sentence splitter for the lättläst per-sentence path. Splits on ., !, ? and
# the Swedish abbreviations are rare in lesson narration; an over-split just
# adds a pause, never drops text (the pieces are re-joined verbatim).
_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]*", re.UNICODE)


class TTSError(RuntimeError):
    """Narration synthesis failed (bad request, transport, or empty audio)."""


class TTSUnavailable(TTSError):
    """The VM600 voice_service could not be reached or has TTS disabled.

    Distinct from :class:`TTSError` so the film pipeline can choose to mock /
    skip narration (or surface a clearer "voice service down" message) rather
    than treat an infra outage like a bad request.
    """


def default_tts_url() -> str:
    """The voice_service base URL (env-overridable). VM600, port 8020."""
    return os.environ.get("LARBILD_TTS_URL", "http://192.168.5.185:8020").rstrip("/")


def is_valid_voice(voice: str) -> bool:
    """True iff ``voice`` is one of the two supported narration voice slugs."""
    return voice in VOICE_PROFILES


class PiperClient:
    """HTTP client for the VM600 voice_service ``/tts`` endpoint.

    One synthesis = ``POST {base}/tts`` with a ``text`` form field, returning a
    ``audio/wav`` body. Injectable in tests via ``post`` (a callable with the
    ``requests.post`` signature) so no network is touched; production uses
    ``requests.post``.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        post: Optional[Callable[..., requests.Response]] = None,
    ):
        self.base_url = (base_url or default_tts_url()).rstrip("/")
        self.timeout_s = timeout_s
        self._post = post or requests.post

    def synth_once(self, text: str, *, length_scale: Optional[float] = None) -> bytes:
        """Synthesise one chunk of text to WAV bytes (a single /tts call).

        Raises :class:`TTSUnavailable` on a transport error or a 503 (TTS off),
        :class:`TTSError` on any other non-200 or an empty body.
        """
        data: dict[str, object] = {"text": text}
        if length_scale is not None:
            # Forward-compat: ignored by today's voice_service (unknown Form
            # field), honoured if VM600 later adds Piper's length_scale.
            data["length_scale"] = length_scale
        try:
            resp = self._post(f"{self.base_url}/tts", data=data, timeout=self.timeout_s)
        except requests.RequestException as e:  # connect/read error = infra down
            raise TTSUnavailable(f"voice_service unreachable at {self.base_url}: {e}") from e

        if resp.status_code == 503:
            raise TTSUnavailable("voice_service reports Piper TTS unavailable (503)")
        if resp.status_code != 200:
            body = (resp.text or "")[:200]
            raise TTSError(f"voice_service /tts HTTP {resp.status_code}: {body}")
        audio = resp.content
        if not audio:
            raise TTSError("voice_service /tts returned an empty body")
        return audio


def _split_sentences(text: str) -> list[str]:
    """Split narration into sentences for the lättläst per-sentence path.

    Returns the trimmed, non-empty pieces in order. Joining them back yields the
    original words (only whitespace between sentences differs), so no text is
    ever dropped — an over-split just inserts an extra pause.
    """
    pieces = [m.group(0).strip() for m in _SENTENCE_RE.finditer(text)]
    return [p for p in pieces if p]


def _concat_wavs(clips: list[bytes], gap_s: float) -> bytes:
    """Concatenate PCM WAV clips, inserting ``gap_s`` of silence between them.

    All clips come from the same Piper voice, so their format (channels, sample
    width, frame rate) matches; this reads the format from the first clip and
    writes one WAV with the joined frames + silence gaps. Pure stdlib ``wave`` —
    no ffmpeg, no numpy — so it runs anywhere the pipeline's tests run.
    """
    if not clips:
        raise TTSError("nothing to concatenate (no audio clips)")
    if len(clips) == 1 and gap_s <= 0:
        return clips[0]

    with wave.open(io.BytesIO(clips[0]), "rb") as w0:
        nchannels = w0.getnchannels()
        sampwidth = w0.getsampwidth()
        framerate = w0.getframerate()

    silence = b"\x00" * int(framerate * max(gap_s, 0.0)) * nchannels * sampwidth

    out = io.BytesIO()
    with wave.open(out, "wb") as ow:
        ow.setnchannels(nchannels)
        ow.setsampwidth(sampwidth)
        ow.setframerate(framerate)
        for i, clip in enumerate(clips):
            with wave.open(io.BytesIO(clip), "rb") as w:
                ow.writeframes(w.readframes(w.getnframes()))
            if gap_s > 0 and i != len(clips) - 1:
                ow.writeframes(silence)
    return out.getvalue()


def wav_duration_s(wav_bytes: bytes) -> float:
    """Duration of a PCM WAV in seconds (frames / frame-rate)."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate() or 1
    return frames / float(rate)


def synthesize(
    text: str,
    voice: str = "sv-standard",
    *,
    client: Optional[PiperClient] = None,
) -> bytes:
    """Synthesise ``text`` to a WAV in the requested narration ``voice``.

    The single public entry point the film pipeline (lb-4vw) calls. Validates
    the voice + text length, then:

      * ``sv-standard`` → one ``/tts`` call, the natural-pace clip.
      * ``sv-lattlast`` → splits the text into sentences, synthesises each, and
        concatenates them with a silence gap (:data:`VOICE_PROFILES`) so the
        pacing is the slower, clearer easy-read the taxonomy describes.

    Returns PCM WAV bytes ready for the ffmpeg mux. ``client`` is injectable for
    tests (a :class:`PiperClient` with a stub ``post``); production builds the
    real VM600 client. Raises :class:`TTSError` / :class:`TTSUnavailable`.
    """
    if not is_valid_voice(voice):
        raise TTSError(
            f"unknown narration voice {voice!r}; valid: {', '.join(NARRATION_VOICES)}"
        )
    text = (text or "").strip()
    if not text:
        raise TTSError("narration text is empty")
    if len(text) > MAX_CHARS:
        raise TTSError(f"narration text too long ({len(text)} > {MAX_CHARS} chars)")

    profile = VOICE_PROFILES[voice]
    client = client or PiperClient()

    if profile.sentence_gap_s <= 0:
        # Standard: a single synthesis preserves Piper's own prosody best.
        return client.synth_once(text, length_scale=profile.length_scale)

    # Lättläst: per-sentence synthesis + silence gaps = slower, clearer pacing.
    sentences = _split_sentences(text) or [text]
    clips = [client.synth_once(s, length_scale=profile.length_scale) for s in sentences]
    return _concat_wavs(clips, profile.sentence_gap_s)
