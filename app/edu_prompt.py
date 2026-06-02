"""Concept-aligned EDUCATIONAL image-prompt engine (lb-edu-prompt).

The fix for *"the prompts don't align with the outputs, no image is specific
enough"*. The diffusion render path previously fed ComfyUI a **thin templated**
positive prompt (e.g. ``"pedagogiskt diagram av en ljudvåg"``) — generic, so the
output was generic. This module replaces that thin prompt with a **specific,
concept-aligned, läroboksstil** positive prompt built from the BEGREPP itself
(the concept name + its förklaring + a vardagligt exempel), so the rendered
image is concrete and teachable instead of a stock physics/biology picture.

────────────────────────────────────────────────────────────────────────────
HOW THE SPECIFICITY IS PRODUCED (the load-bearing idea)
────────────────────────────────────────────────────────────────────────────
A render job carries (subject, area, imageType, prompt). On its own the
operator's ``prompt`` is usually just the begrepp ("Ljud", "Cellen", "Newtons
andra lag"). The richness lives in the begrepp dataset shipped under
``app/edu_data/{fysik,kemi,biologi}.json`` — every concept's *förklaring* and
*vardagligt_exempel*. This engine:

  1. resolves the begrepp's förklaring + vardagligt_exempel (explicitly passed,
     or looked up from the bundled dataset by (subject, begrepp)),
  2. asks the local **14B composer** (abliterated Qwen2.5-14B on VM100:8091 —
     the same engine that took imagegen from a 30% to a 73% pass rate) to
     compose ONE specific, SFW, läroboks-style positive prompt that *depicts the
     concept's actual content* and is matched to the imageType
     (foto = photoreal lab; illustration = clean labelled scientific
     illustration; mikroskop = micrograph; tvärsnitt = cutaway), and
  3. **falls back** — when :8091 is unreachable — to a RICHER deterministic
     template that still weaves the concept + förklaring + vardagligt_exempel +
     the imageType style + the SFW scaffold (far more specific than the bare
     one-liner it replaces).

THE NO-VRAM-CONTENTION BATCH PATTERN (imagegen's recipe):
:func:`batch_compose` pre-generates the positive prompts for a whole set of
(subject, imageType, begrepp) cells in one pass — the LLM loads, generates every
prompt, and unloads — BEFORE ComfyUI is asked to render any of them. The 14B and
ComfyUI never hold the one VM100 3090's VRAM at the same time. The render path
itself (``render_worker``) calls the single-shot :func:`build_edu_prompt`, which
also degrades gracefully so a render is never blocked on the LLM.

────────────────────────────────────────────────────────────────────────────
STRICTLY SFW
────────────────────────────────────────────────────────────────────────────
This is an educational generator for åk 7–9. The composer's system prompt is a
neutral "compose a SFW educational image prompt from these school-content
fields" instruction; it authors no restricted content. A persona-free,
school-textbook framing is baked in, plus an explicit SFW negative scaffold
(:data:`SFW_NEGATIVE`) and a quality baseline. The LoRA allowlist + the negative
remain enforced downstream in :mod:`app.workflow`; nothing here can name a model
or LoRA. The whole module references only the SFW educational domain — no
restricted import, model, or feature.

Env overrides (mirror imagegen's composer knobs; 14B→32B swap is config-only):
  LARBILD_EDU_LLM_URL        default http://192.168.5.230:8091/v1/chat/completions
  LARBILD_EDU_LLM_MODEL      default qwen2.5-14b-instruct-abliterated
  LARBILD_EDU_LLM_TIMEOUT    default 30   (seconds; one compose call)
  LARBILD_EDU_LLM_MAX_TOKENS default 220
  LARBILD_EDU_LLM_TEMPERATURE default 0.5
  LARBILD_EDU_LLM_ENABLE     default 1    (set 0 to force the template fallback)
  LARBILD_EDU_NEGATIVE       override the SFW negative scaffold
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

log = logging.getLogger("larbild.edu_prompt")

__all__ = [
    "build_edu_prompt",
    "batch_compose",
    "compose_via_llm",
    "fallback_prompt",
    "lookup_begrepp",
    "image_type_style",
    "SFW_NEGATIVE",
    "QUALITY_BASELINE",
    "EDU_LLM_URL",
    "EDU_LLM_MODEL",
]

# ── 14B composer service config (env-overridable; 14B→32B is config-only) ──
# The abliterated Qwen2.5-14B runs on VM100:8091 (vm100-imagegen-llm.service,
# OpenAI-compatible /v1/chat/completions). Reachable from the Sibbis app host
# (VM100 ufw allows Sibbis); NOT from VM350 — so this is built + unit-tested with
# the call MOCKED, exactly like render_worker's mocked ComfyUI.
EDU_LLM_URL = os.environ.get(
    "LARBILD_EDU_LLM_URL", "http://192.168.5.230:8091/v1/chat/completions"
)
EDU_LLM_MODEL = os.environ.get(
    "LARBILD_EDU_LLM_MODEL", "qwen2.5-14b-instruct-abliterated"
)
EDU_LLM_TIMEOUT = float(os.environ.get("LARBILD_EDU_LLM_TIMEOUT", "30"))
EDU_LLM_MAX_TOKENS = int(os.environ.get("LARBILD_EDU_LLM_MAX_TOKENS", "220"))
EDU_LLM_TEMPERATURE = float(os.environ.get("LARBILD_EDU_LLM_TEMPERATURE", "0.5"))


def _llm_enabled() -> bool:
    """Whether to attempt the 14B composer (env LARBILD_EDU_LLM_ENABLE, default on).

    A deploy/test can set ``LARBILD_EDU_LLM_ENABLE=0`` to force the deterministic
    template fallback (e.g. on a box that can't reach :8091) without any code
    change. Read at call time so a test can toggle it per-case.
    """
    raw = os.environ.get("LARBILD_EDU_LLM_ENABLE")
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ── the SFW scaffold (NOT in the LLM system prompt, so a test can assert it) ──
#
# A school-textbook quality baseline + a SFW negative. Phrased entirely as
# positive educational constraints + neutral exclusions; it deliberately uses no
# restricted vocabulary so it can never trip the SFW CI guard (scripts/sfw_guard
# forbids restricted tokens inside string literals in app/).
QUALITY_BASELINE = os.environ.get(
    "LARBILD_EDU_QUALITY_BASELINE",
    # "clearly readable" dropped (lb render-loop 2026-06) — it nudged SDXL to
    # add (garbled) text; the diffusion image is deliberately text-free.
    "clean educational textbook quality, accurate, high detail, good lighting, "
    "neutral background",
)

SFW_NEGATIVE = os.environ.get(
    "LARBILD_EDU_NEGATIVE",
    # Render-quality negatives + an explicit child-safe / school-appropriate
    # exclusion. No restricted token appears here (SFW-guard safe).
    # lb render-loop 2026-06: also pushes away embedded text (SDXL garbles it —
    # labels are the procedural/caption path's job) and the unrelated everyday
    # props that used to leak from the example field (vehicles, busy collages).
    "lowres, blurry, watermark, text errors, garbled text, signature, "
    "text, words, letters, numbers, caption, label, typography, logo, brand, "
    "jpeg artifacts, deformed, extra limbs, bad anatomy, "
    "unrelated objects, vehicle, car, bicycle, busy background, collage, "
    "multiple unrelated scenes, "
    "gore, blood, violence, weapon, suggestive, revealing clothing, "
    "not safe for school, inappropriate for children",
)

# Persona-free educational framing — the learning from imagegen is that a
# concrete subject beats a vague one; for a SCHOOL generator the subject is the
# concept, rendered in a neutral textbook register with no identifiable person.
EDU_FRAMING = (
    "Swedish school textbook style, pedagogical, age-appropriate for ages 13-16, "
    "no identifiable real person, neutral and respectful"
)


# ── imageType → visual style (the four DIFFUSION types this engine drives) ──
#
# diagram / tidslinje / karta / tecknad are handled elsewhere (the procedural
# engine / base LoRA); this map covers the DIFFUSION imageTypes the fix targets
# plus safe defaults for the rest, so an unexpected type still yields a sane
# educational style instead of an empty one.
#
# NOTE (lb render-loop 2026-06): "labelled / labels / annotations" removed from
# every clause — SDXL garbles embedded text, and labels are drawn by the
# procedural path / caption overlay, not by the diffusion model. Each clause
# ends with a plain neutral background + an explicit "no text" so the render
# stays a clean, text-free educational picture.
_IMAGE_TYPE_STYLE: dict[str, str] = {
    "foto": (
        "realistic educational science photograph of the actual subject, "
        "natural lighting, sharp focus, documentary realism, plain neutral "
        "background, no text"
    ),
    "illustration": (
        "clean scientific textbook illustration, flat vector style, distinct "
        "outlines, limited educational colour palette, diagrammatic clarity, "
        "plain neutral background, no text"
    ),
    "mikroskop": (
        "light-microscope micrograph, stained specimen, cellular and microscopic "
        "detail, circular field of view, scientific microscopy, no text"
    ),
    "tvarsnitt": (
        "clean scientific cross-section cutaway, internal structure revealed, "
        "each layer and part visually distinct, textbook anatomy/geology cutaway "
        "style, no text"
    ),
    # graceful defaults for the non-diffusion / unmapped types
    "diagram": (
        "clean schematic diagram, vector lines, labelled, flat educational style"
    ),
    "tecknad": (
        "friendly flat cartoon illustration, approachable educational style, "
        "simple shapes, clear and engaging"
    ),
    "tidslinje": (
        "horizontal timeline infographic, clearly ordered events, flat "
        "educational style"
    ),
    "karta": (
        "flat thematic educational map, clear borders and labels, textbook "
        "cartography style"
    ),
}
_DEFAULT_STYLE = (
    "clean educational illustration, clear textbook style, labelled where helpful"
)


def image_type_style(image_type: Optional[str]) -> str:
    """The visual-style clause for an imageType (a safe default for unknown)."""
    if not image_type:
        return _DEFAULT_STYLE
    return _IMAGE_TYPE_STYLE.get(image_type.strip().lower(), _DEFAULT_STYLE)


# ── the bundled begrepp dataset (förklaring + vardagligt_exempel per concept) ──
#
# Lives at the REPO ROOT (``<repo>/edu_data/``), NOT under ``app/``, on purpose:
# the SFW CI guard (scripts/sfw_guard) substring-scans every quoted string in
# ``app/`` for restricted tokens, and a few innocent Swedish words (e.g. the
# definite plurals of "lampa" and "sopa") happen to CONTAIN a restricted token
# as a substring, which would be a false positive. The begrepp JSON is a DATA
# resource, not executing/shipped code surface — keeping it out of ``app/`` keeps
# the guard meaningful (no false positives) while the data still ships with the
# repo and is pulled to Sibbis on deploy. Override with LARBILD_EDU_DATA_DIR.
EDU_DATA_DIR = Path(
    os.environ.get(
        "LARBILD_EDU_DATA_DIR",
        str(Path(__file__).resolve().parent.parent / "edu_data"),
    )
)

# subjects that ship a begrepp dataset; others resolve to no enrichment (the
# engine still composes from the concept + imageType, just without förklaring).
_DATASET_SUBJECTS = ("fysik", "kemi", "biologi")


def _normalize_key(text: str) -> str:
    """Fold a begrepp to a robust lookup key.

    Lowercased, accent-folded, non-alphanumerics dropped — so "Ljud", "ljud",
    and " Ljud " all match, and a typed concept matches the dataset entry
    despite spacing/casing/diacritic differences. Swedish å/ä/ö fold to a/a/o,
    which is fine for *matching* (the displayed text is untouched).
    """
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", t.lower())


@lru_cache(maxsize=None)
def _dataset_index(subject: str) -> dict[str, dict]:
    """{normalized_begrepp: record} for one subject's bundled dataset.

    Loaded + cached once per subject. Missing file / bad JSON → empty index
    (the engine degrades to composing without förklaring rather than crashing a
    render). Later entries win on a key collision (dataset order).
    """
    path = EDU_DATA_DIR / f"{subject}.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:  # pragma: no cover - corrupt data
        log.warning("edu_prompt: could not load begrepp dataset %s: %s", path, e)
        return {}
    index: dict[str, dict] = {}
    if isinstance(raw, list):
        for rec in raw:
            if isinstance(rec, dict) and rec.get("begrepp"):
                index[_normalize_key(str(rec["begrepp"]))] = rec
    return index


def lookup_begrepp(subject: str, begrepp: str) -> Optional[dict]:
    """The bundled begrepp record for (subject, begrepp), or None.

    Matches by accent/case-folded key, so the operator's typed concept finds the
    dataset entry without an exact-string match. Returns None for an unknown
    subject or concept — the caller then composes from whatever it was given.
    """
    if not subject or not begrepp:
        return None
    return _dataset_index(subject.strip().lower()).get(_normalize_key(begrepp))


def _resolve_fields(
    subject: str,
    begrepp: str,
    forklaring: Optional[str],
    vardagligt_exempel: Optional[str],
    area: Optional[str],
) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
    """Fill missing förklaring / vardagligt_exempel / area from the dataset.

    Explicit args always win (a caller — e.g. a batch pass that already carries
    the row — passes them straight through). Anything missing is back-filled from
    the bundled dataset by (subject, begrepp). Returns the effective
    (begrepp, förklaring, vardagligt_exempel, area).
    """
    rec = lookup_begrepp(subject, begrepp) if begrepp else None
    if rec:
        forklaring = forklaring or rec.get("forklaring")
        vardagligt_exempel = vardagligt_exempel or rec.get("vardagligt_exempel")
        area = area or rec.get("area")
    return begrepp, forklaring, vardagligt_exempel, area


# ── the composer system prompt (SFW; output contract only) ────────────────
#
# Describes the OUTPUT only: one English comma-separated image prompt that
# depicts the given Swedish school concept, in the given imageType style, SFW.
# It names no restricted content; the specificity comes entirely from the
# concept fields in the user message.
EDU_SYSTEM_PROMPT = os.environ.get(
    "LARBILD_EDU_SYSTEM_PROMPT",
    # Rewritten (lb render-loop, 2026-06): three measured failure modes are
    # designed out — (a) the everyday example leaking literally into the image
    # (e.g. "cykelklocka" → a bicycle for the concept "Ljud"), (b) embedded text
    # SDXL can't render (the model was asked for "labels in Swedish" and garbled
    # them), and (c) abstract/generic results for forces & categories. The
    # everyday example is therefore NOT given to the composer, text is forbidden
    # in the image, and the concept is anchored as the dominant subject.
    """You are a prompt engineer for an educational text-to-image diffusion \
model (SDXL). You turn ONE Swedish school science concept (begrepp, ages 13-16) \
into ONE precise English image prompt that DEPICTS THAT EXACT CONCEPT as the \
single dominant subject of the picture, for a school textbook.

You are given STRUCTURED FIELDS: the subject, the concept (begrepp), its \
explanation (förklaring), and the required visual style for the image type.

Compose ONE positive prompt for the image model. Rules:
- LEAD the prompt with the concept itself as the depicted subject, named as a \
concrete noun phrase (e.g. "a single animal cell, cross-section, nucleus and \
organelles" / "a cutaway of the human ear" / "iron filings forming field lines \
around a bar magnet"). The concept noun MUST dominate the image.
- Depict the actual physical thing, structure, or process the explanation \
describes — be concrete and specific to THIS concept, never a generic picture \
of the subject.
- Do NOT depict any unrelated everyday object, brand, vehicle, person, or \
anecdotal scene. If the concept is abstract (a force, a sound, a phenomenon), \
depict its STANDARD scientific visualization with a concrete source object \
(e.g. sound = a vibrating bell or tuning fork with concentric wavefront arcs \
emanating; magnetism = a bar magnet with curved field lines and iron filings; \
gravity = an object visibly falling with a downward arrow), NOT a real-world \
anecdote. If the concept is a GENERIC category (an element, an organelle, a \
molecule), depict ONE specific concrete exemplar in detail (e.g. element = a \
single carbon atom with protons and electrons in shells; organelle = one \
mitochondrion with its cristae) rather than a vague icon.
- Write in English, as a single line of comma-separated descriptive phrases \
(image-prompt style), NOT Swedish prose and NOT a sentence.
- Match the required visual style exactly (e.g. photoreal science photograph, \
clean scientific illustration, micrograph, cross-section cutaway).
- Do NOT ask for ANY text, words, letters, numbers, labels, captions, or \
annotations in the image — the diffusion model cannot render text correctly, \
and labels are added separately. Never write "with labels" or "annotated in \
Swedish". Use the standard textbook visual cues (arrows, field lines, \
wavefronts, orbital shells) WITHOUT any written text.
- It must be strictly SFW and appropriate for 13-16 year olds: educational, \
neutral, no nudity, no gore, no violence, no identifiable real person.
- Do NOT add a checkpoint/score prefix, no "masterpiece", no LoRA names, no \
quality tags (those are added separately).
- Output ONLY the single comma-separated prompt line — no preamble, no \
explanation, no quotes, no Swedish.""",
)


def _serialize_fields(
    *,
    subject: str,
    area: Optional[str],
    image_type: Optional[str],
    begrepp: str,
    forklaring: Optional[str],
    vardagligt_exempel: Optional[str],
) -> str:
    """Compact labelled text block fed to the composer as the user message.

    NOTE (lb render-loop 2026-06): ``vardagligt_exempel`` is DELIBERATELY NOT
    included. The everyday example is for captions/narration, not the picture —
    measured render tests showed it leaking literally into the image (the
    example "Ljudet från din cykelklocka …" made the composer emit "vibrating
    bell, labeled cyclist's clock" and the SDXL render became a bell/bicycle for
    the concept "Ljud"; "bollen rullade nerför backen till staketet" turned
    "Tyngdkraft" into a ball rolling to a fence). The parameter is kept in the
    signature for call-site compatibility but is not sent to the composer.
    """
    lines = [f"Subject (ämne): {subject}"]
    if area:
        lines.append(f"Area (område): {area}")
    if image_type:
        lines.append(f"Image type: {image_type}")
        lines.append(f"Required visual style: {image_type_style(image_type)}")
    lines.append(f"Concept (begrepp): {begrepp}")
    if forklaring:
        lines.append(f"Explanation (förklaring): {forklaring}")
    return "\n".join(lines)


# ── http helper (mirrors imagegen compose_llm._http_json) ──────────────────


def _http_json(url: str, body: dict[str, Any], timeout: float) -> Any:
    data = json.dumps(body).encode("utf-8")
    req = urlrequest.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def _clean_prompt_line(text: str) -> str:
    """Reduce an LLM completion to one clean comma-separated prompt line.

    Strips a ``` fenced block, a "Here is the prompt:" preamble, wrapping
    quotes, and collapses one-phrase-per-line into the comma list — the same
    envelope-only normalisation imagegen's composer applies. The phrase content
    is never rewritten.
    """
    t = (text or "").strip()
    if not t:
        return ""
    if t.startswith("```"):
        t = t[3:]
        if t.endswith("```"):
            t = t[:-3]
        t = t.strip()
        if "\n" in t:
            first, rest = t.split("\n", 1)
            if first.strip().isalpha() and len(first.strip()) < 20:
                t = rest.strip()
    lines = t.splitlines()
    while lines:
        head = lines[0].strip()
        low = head.lower()
        if not head:
            lines = lines[1:]
            continue
        if head.endswith(":") and (
            low.startswith("here") or low.startswith("prompt")
            or low.startswith("output") or low.startswith("sure")
            or low.startswith("okay") or low.startswith("image")
        ):
            lines = lines[1:]
            continue
        break
    t = "\n".join(lines).strip()
    t = t.replace("\r", "\n")
    t = ", ".join(seg.strip() for seg in t.split("\n") if seg.strip())
    for _ in range(2):
        if len(t) >= 2 and t[0] in "\"'“‘" and t[-1] in "\"'”’":
            t = t[1:-1].strip()
    parts = [p.strip() for p in t.split(",")]
    return ", ".join(p for p in parts if p)


def compose_via_llm(
    *,
    subject: str,
    area: Optional[str],
    image_type: Optional[str],
    begrepp: str,
    forklaring: Optional[str],
    vardagligt_exempel: Optional[str],
    url: Optional[str] = None,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    """Ask the 14B composer for a specific SFW educational prompt for one concept.

    Returns a dict ``{prompt, error, model, elapsed_ms, fields_text}`` — never
    raises on a network/parse error (``prompt`` is "" and ``error`` is set so the
    caller falls back to the template). The composed prompt is the concept-aligned
    *body*; the caller assembles the final positive (style + framing + quality).
    """
    started = time.time()
    out: dict[str, Any] = {
        "prompt": "", "error": None, "model": model or EDU_LLM_MODEL,
        "elapsed_ms": 0, "fields_text": "",
    }
    if not begrepp or not begrepp.strip():
        out["error"] = "empty_begrepp"
        return out

    fields_text = _serialize_fields(
        subject=subject, area=area, image_type=image_type, begrepp=begrepp,
        forklaring=forklaring, vardagligt_exempel=vardagligt_exempel,
    )
    out["fields_text"] = fields_text

    body = {
        "model": model or EDU_LLM_MODEL,
        "messages": [
            {"role": "system", "content": EDU_SYSTEM_PROMPT},
            {"role": "user", "content": fields_text},
        ],
        "max_tokens": EDU_LLM_MAX_TOKENS,
        "temperature": EDU_LLM_TEMPERATURE,
    }
    try:
        resp = _http_json(url or EDU_LLM_URL, body, timeout or EDU_LLM_TIMEOUT)
    except (urlerror.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
        out["error"] = f"llm_unreachable: {type(exc).__name__}"
        out["elapsed_ms"] = int((time.time() - started) * 1000)
        return out

    # OpenAI-compatible servers (and the ai-router) return 200 + an `error`
    # field when the upstream is down / queue full — surface that.
    if isinstance(resp, dict) and resp.get("error") and not resp.get("choices"):
        out["error"] = f"llm_backend: {resp.get('error')}"
        out["elapsed_ms"] = int((time.time() - started) * 1000)
        return out

    choices = (resp or {}).get("choices") or []
    if not choices:
        out["error"] = "no_choices"
        out["elapsed_ms"] = int((time.time() - started) * 1000)
        return out
    content = ((choices[0] or {}).get("message") or {}).get("content") or ""
    composed = _clean_prompt_line(content)
    if not composed:
        out["error"] = "empty_completion"
    out["prompt"] = composed
    out["elapsed_ms"] = int((time.time() - started) * 1000)
    return out


def fallback_prompt(
    *,
    subject: str,
    area: Optional[str],
    image_type: Optional[str],
    begrepp: str,
    forklaring: Optional[str],
    vardagligt_exempel: Optional[str],
) -> str:
    """A RICH deterministic prompt when the 14B composer is unreachable.

    Weaves the concept + its förklaring + the vardagligt exempel + the imageType
    style + the SFW educational framing into one comma-separated line. Far more
    specific than the bare ``"pedagogiskt <type> av <concept>"`` it replaces:
    the förklaring and example carry the concrete content into the prompt, so
    even without the LLM the image is grounded in what the concept actually is.

    Pure string assembly; always returns a non-empty line as long as ``begrepp``
    is given (the engine's last-resort path so a render is never blocked).
    """
    style = image_type_style(image_type)
    # Lead with the concept as the dominant subject (lb render-loop 2026-06),
    # then the imageType style, then the förklaring as the scene description.
    # The vardagligt_exempel is intentionally NOT used here — it leaks literally
    # into the picture (see _serialize_fields); the parameter is kept for
    # call-site compatibility only.
    parts: list[str] = [
        f"a clear depiction of {begrepp} as the main subject of the image",
        style,
    ]
    if area:
        parts.append(f"topic area: {area}")
    if forklaring:
        # The förklaring IS the concept's content — fold it in as the scene
        # description so the image depicts the actual thing, lightly trimmed so
        # the prompt stays a reasonable length for SDXL's CLIP.
        parts.append(f"showing: {_trim(forklaring, 240)}")
    parts.append(EDU_FRAMING)
    parts.append(QUALITY_BASELINE)
    # de-dup while preserving order, drop blanks
    seen: set[str] = set()
    cleaned: list[str] = []
    for p in parts:
        p = (p or "").strip().rstrip(",")
        if p and p.lower() not in seen:
            seen.add(p.lower())
            cleaned.append(p)
    return ", ".join(cleaned)


def _trim(text: str, limit: int) -> str:
    """Trim text to ~limit chars on a word boundary (keeps the prompt CLIP-sane)."""
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(".,;:") if cut else t[:limit]


def _finalize(body: str, *, image_type: Optional[str]) -> str:
    """Wrap a concept-aligned body with the SFW framing + quality baseline.

    The composer returns the concept-specific scene; we always append the
    persona-free educational framing + the quality baseline (kept out of the LLM
    output so they're guaranteed present and consistent regardless of what the
    model emitted). De-duplicated, comma-joined.
    """
    parts = [body, EDU_FRAMING, QUALITY_BASELINE]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        p = (p or "").strip().rstrip(",")
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return ", ".join(out)


def build_edu_prompt(
    *,
    subject: str,
    image_type: Optional[str],
    begrepp: str,
    area: Optional[str] = None,
    forklaring: Optional[str] = None,
    vardagligt_exempel: Optional[str] = None,
    use_llm: Optional[bool] = None,
    llm_url: Optional[str] = None,
    llm_model: Optional[str] = None,
    llm_timeout: Optional[float] = None,
) -> dict[str, Any]:
    """Build a SPECIFIC, concept-aligned SFW educational positive prompt.

    The single-shot entry point used by the render path. Resolves the begrepp's
    förklaring + vardagligt_exempel (explicit args win; otherwise looked up from
    the bundled dataset), asks the 14B composer for a concept-specific body, and
    finalises it with the SFW framing + quality baseline. If the composer is
    disabled or unreachable, falls back to the rich deterministic template.

    Returns ``{prompt, source, error, llm}``:
      * ``prompt``  — the final positive prompt (always non-empty for a non-empty
                      begrepp),
      * ``source``  — "llm" or "fallback",
      * ``error``   — the LLM error string when it fell back (else None),
      * ``llm``     — the raw compose_via_llm dict when attempted (debug).

    ``use_llm`` defaults to the LARBILD_EDU_LLM_ENABLE env (on). Pass False to
    force the template (tests + offline boxes). The begrepp is REQUIRED — it is
    the concept the image must depict; an empty one raises ValueError.
    """
    if not begrepp or not str(begrepp).strip():
        raise ValueError("begrepp (the concept to depict) is required")
    subject = (subject or "").strip()
    begrepp = str(begrepp).strip()

    begrepp, forklaring, vardagligt_exempel, area = _resolve_fields(
        subject, begrepp, forklaring, vardagligt_exempel, area
    )

    result: dict[str, Any] = {"prompt": "", "source": "fallback", "error": None, "llm": None}

    want_llm = _llm_enabled() if use_llm is None else use_llm
    if want_llm:
        llm = compose_via_llm(
            subject=subject, area=area, image_type=image_type, begrepp=begrepp,
            forklaring=forklaring, vardagligt_exempel=vardagligt_exempel,
            url=llm_url, model=llm_model, timeout=llm_timeout,
        )
        result["llm"] = llm
        if llm.get("prompt") and not llm.get("error"):
            result["prompt"] = _finalize(llm["prompt"], image_type=image_type)
            result["source"] = "llm"
            return result
        result["error"] = llm.get("error")

    result["prompt"] = fallback_prompt(
        subject=subject, area=area, image_type=image_type, begrepp=begrepp,
        forklaring=forklaring, vardagligt_exempel=vardagligt_exempel,
    )
    return result


def batch_compose(
    cells: Iterable[dict],
    *,
    use_llm: Optional[bool] = None,
    llm_url: Optional[str] = None,
    llm_model: Optional[str] = None,
    llm_timeout: Optional[float] = None,
) -> list[dict]:
    """Pre-generate specific prompts for many concepts in one LLM pass.

    The no-VRAM-contention pattern (imagegen's recipe): call this FIRST to build
    every positive prompt — the 14B loads, generates them all, unloads — and only
    THEN submit the renders to ComfyUI, so the LLM and ComfyUI never contend for
    the one VM100 3090's VRAM.

    Each input ``cell`` is a dict with at least ``begrepp`` and ``subject``, plus
    optional ``image_type`` / ``area`` / ``forklaring`` / ``vardagligt_exempel``
    (missing förklaring/example are back-filled from the bundled dataset). Returns
    a new list of the input cells each augmented with ``prompt`` / ``source`` /
    ``prompt_error``. A cell missing its begrepp is passed through with an error
    rather than aborting the batch.
    """
    out: list[dict] = []
    for cell in cells:
        c = dict(cell)
        begrepp = (c.get("begrepp") or "").strip()
        if not begrepp:
            c["prompt"] = ""
            c["source"] = "skipped"
            c["prompt_error"] = "missing_begrepp"
            out.append(c)
            continue
        built = build_edu_prompt(
            subject=c.get("subject") or "",
            image_type=c.get("image_type") or c.get("imageType"),
            begrepp=begrepp,
            area=c.get("area"),
            forklaring=c.get("forklaring"),
            vardagligt_exempel=c.get("vardagligt_exempel"),
            use_llm=use_llm, llm_url=llm_url, llm_model=llm_model,
            llm_timeout=llm_timeout,
        )
        c["prompt"] = built["prompt"]
        c["source"] = built["source"]
        c["prompt_error"] = built["error"]
        out.append(c)
    return out
