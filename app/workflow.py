"""SFW txt2img workflow assembly + LoRA injection (lb-c2r, P1.1).

Forks the *pattern* of imagegen's ``sibbis-app/video.py`` workflow assembly —
a wrapped ``${VAR}`` placeholder template, recursive substitution, and LoRA
injection as ComfyUI graph nodes — but for Lärbild's case: a **single-image
SDXL txt2img** graph (imagegen's templates are img2vid). The graph is the
canonical SDXL still chain:

    CheckpointLoaderSimple → LoraLoader → CLIPTextEncode(pos/neg)
        → EmptyLatentImage → KSampler → VAEDecode → SaveImage

THE SFW INJECTION GUARD (the security boundary this bead exists to enforce):

  * The LoRA is **never** taken from the caller. It is resolved server-side from
    the ``(subject, imageType)`` selection via :func:`app.taxonomy.resolve_lora`
    — backend-authoritative, exactly like the ``/api/resolve-lora`` endpoint
    (lb-9tz). The funnel can ask for a subject + image type; it can never name a
    LoRA.
  * Before a resolved LoRA is injected into the graph, its name is checked
    against the **allowlist** = every name the taxonomy SoT declares
    (``baseLora`` ∪ ``loraMatrix``). A name not in that set is **rejected** with
    :class:`LoraNotAllowedError` and nothing is submitted. ``resolve_lora`` only
    ever returns names from those two sources, so the check always passes on the
    happy path; it makes the SFW-isolation invariant *verifiable* and *fails
    closed* — an off-allowlist LoRA can never reach ComfyUI, even via a crafted
    request that somehow smuggled a name through (BUILD_PLAN hard-isolation
    plane: reject any LoRA not in baseLora ∪ loraMatrix so a restricted-model
    injection is impossible even via a crafted request). It is the same guard the
    resolve-lora endpoint applies, enforced here at the point a LoRA actually
    enters a render.

SFW-only: the only checkpoints/LoRAs this can ever reference are the
taxonomy-declared SFW set; no restricted import, model, or feature appears here.

Env overrides:
  LARBILD_SDXL_CHECKPOINT   default sdxl-edu-base.safetensors (the SFW base
                            checkpoint; the actual file lands with P9 model
                            sourcing — until then renders are deferred, lb-ocx).
  LARBILD_LORA_SUFFIX       default .safetensors (appended to a logical LoRA
                            name to form the ComfyUI lora_name on disk).
"""
from __future__ import annotations

import copy
import os
import re
from functools import lru_cache
from typing import Any, Optional

from . import taxonomy

__all__ = [
    "LoraNotAllowedError",
    "WorkflowError",
    "allowlist",
    "flux_allowlist",
    "resolve_and_validate_lora",
    "resolve_and_validate_flux_lora",
    "assemble_txt2img",
    "assemble_flux_txt2img",
    "lora_filename",
]

PLACEHOLDER_RE = re.compile(r"^\$\{([A-Z0-9_]+)\}$")

# Defaults; the actual checkpoint file is sourced in P9. Until it exists, the
# graph is still assemblable + testable — only the *live* render is deferred.
DEFAULT_CHECKPOINT = os.environ.get("LARBILD_SDXL_CHECKPOINT", "sdxl-edu-base.safetensors")
LORA_SUFFIX = os.environ.get("LARBILD_LORA_SUFFIX", ".safetensors")

# ── FLUX render-lane defaults (lb FLUX lane) ───────────────────────────────
# The FLUX dev-fp8 lane breaks the SDXL prompt-adherence ceiling on abstract /
# science-illustration concepts. flux1-dev-fp8 is a Comfy-Org all-in-one fp8
# checkpoint loaded via CheckpointLoaderSimple (its MODEL output is sampled); the
# CLIP comes from an explicit DualCLIPLoader(t5xxl_fp16, clip_l, type=flux) and
# the VAE from VAELoader(ae.safetensors) — the canonical FLUX text/vae stack, all
# present on VM100. FLUX needs cfg≈1 + a FluxGuidance node (≈3.5) + an
# EmptySD3LatentImage (16-channel latent, NOT SDXL's EmptyLatentImage) + a
# low-step euler/simple schedule. These defaults were validated by a live
# one-image smoke render on VM100 (100s, clean micrograph) before this code
# landed. Every value is env-overridable so a model/encoder swap is config-only.
FLUX_CHECKPOINT = os.environ.get("LARBILD_FLUX_CHECKPOINT", "FLUX1/flux1-dev-fp8.safetensors")
FLUX_CLIP_T5 = os.environ.get("LARBILD_FLUX_CLIP_T5", "t5xxl_fp16.safetensors")
FLUX_CLIP_L = os.environ.get("LARBILD_FLUX_CLIP_L", "clip_l.safetensors")
FLUX_VAE = os.environ.get("LARBILD_FLUX_VAE", "ae.safetensors")
FLUX_DEFAULT_STEPS = int(os.environ.get("LARBILD_FLUX_STEPS", "20"))
FLUX_DEFAULT_GUIDANCE = float(os.environ.get("LARBILD_FLUX_GUIDANCE", "3.5"))
# FLUX is a guidance-distilled model: the KSampler cfg MUST be ~1.0 (the prompt
# strength is carried by FluxGuidance, not cfg). A cfg>1 doubles the forward pass
# and degrades FLUX output — do not raise it.
FLUX_DEFAULT_CFG = float(os.environ.get("LARBILD_FLUX_CFG", "1.0"))
FLUX_DEFAULT_SAMPLER = os.environ.get("LARBILD_FLUX_SAMPLER", "euler")
FLUX_DEFAULT_SCHEDULER = os.environ.get("LARBILD_FLUX_SCHEDULER", "simple")

# Sensible SDXL still defaults; every value is overridable per-job via params.
DEFAULT_WIDTH = 1024
DEFAULT_HEIGHT = 1024
DEFAULT_STEPS = 28
DEFAULT_CFG = 6.5
DEFAULT_SAMPLER = "dpmpp_2m"
DEFAULT_SCHEDULER = "karras"
DEFAULT_NEG_PROMPT = (
    "lowres, blurry, watermark, text, signature, jpeg artifacts, deformed"
)
# SFW SaveImage prefix. ComfyUI on VM100 has output/ symlinked to the shared
# vm153 NFS root (/mnt/vm153-output), where imagegen also writes — a bare prefix
# dropped raw saves at that shared root next to imagegen's dirs. Nesting the
# prefix under larbild/raw/ makes ComfyUI write into larbild's OWN isolated
# folder (the subfolder is created relative to ComfyUI's output dir), keeping
# the raw ComfyUI byproducts inside larbild/ and off the shared root. The
# worker's downloaded copy (auto-sorted under STORAGE_DIR/output/...) stays
# authoritative; these raw saves are a redundant ComfyUI-side byproduct kept for
# provenance.
SAVE_PREFIX = "larbild/raw/larbild"


class WorkflowError(RuntimeError):
    """The workflow could not be assembled (bad/missing parameters)."""


class LoraNotAllowedError(WorkflowError):
    """A LoRA was rejected because its name is not in the SFW allowlist.

    Raised before anything is submitted to ComfyUI. The allowlist is the set of
    every LoRA name the taxonomy SoT declares (``baseLora`` ∪ ``loraMatrix``);
    a name outside it cannot enter a render — the SFW injection guard.
    """

    def __init__(self, name: str):
        self.lora_name = name
        super().__init__(
            f"LoRA {name!r} is not in the SFW allowlist "
            f"(baseLora ∪ loraMatrix); refusing to inject it"
        )


@lru_cache(maxsize=1)
def allowlist() -> frozenset[str]:
    """Every LoRA name the taxonomy SoT declares — base LoRAs ∪ matrix cells.

    The single set a render may use (BUILD_PLAN hard-isolation plane). Built once
    from the frozen taxonomy; identical to the ``/api/resolve-lora`` endpoint's
    allowlist so the two enforcement points agree by construction.
    """
    names = {b["name"] for b in taxonomy.BASE_LORA.values()}
    names |= {c["name"] for c in taxonomy.LORA_MATRIX.values()}
    return frozenset(names)


@lru_cache(maxsize=1)
def flux_allowlist() -> frozenset[str]:
    """Every SFW FLUX LoRA name the FLUX lane may inject.

    The FLUX twin of :func:`allowlist`: the set of every name in
    ``taxonomy.FLUX_LORA`` (the Lärbild-allowlisted FLUX adapters). The FLUX
    render path validates against THIS set, so an off-list FLUX LoRA — e.g. one
    of imagegen's NSFW FLUX adapters that also live on the box — can never enter a
    FLUX render. Strictly SFW educational, the same fail-closed guard as the SDXL
    lane, on a separate set because the two lanes' adapters are not
    interchangeable.
    """
    return taxonomy.flux_lora_names()


def lora_filename(name: str) -> str:
    """The on-disk ComfyUI ``lora_name`` for a logical LoRA name.

    Logical names (``photoreal-edu-v3``) map to ``<name>.safetensors`` files in
    the SFW-only LoRA dir (P9). A name that already carries a known weight suffix
    is passed through unchanged.
    """
    if name.endswith((".safetensors", ".ckpt", ".pt", ".bin")):
        return name
    return f"{name}{LORA_SUFFIX}"


def resolve_and_validate_lora(subject: str, image_type: str) -> dict:
    """Backend-authoritative LoRA for a ``(subject, imageType)`` selection.

    Validates both slugs against the taxonomy SoT, resolves the LoRA via
    :func:`app.taxonomy.resolve_lora`, then enforces the SFW allowlist. The
    returned dict is the resolved LoRA (``{name, strength, source, …}``) plus a
    derived ``file`` (the ComfyUI ``lora_name``). Raises:

      * :class:`WorkflowError` — unknown subject or image type, or (defensively)
        a null resolution.
      * :class:`LoraNotAllowedError` — the resolved name is off-allowlist (must
        not happen via ``resolve_lora``; fails closed if the invariant breaks).

    This is the single choke point: every render path calls it, so a LoRA only
    ever enters a graph after passing the allowlist.
    """
    if not subject or not image_type:
        raise WorkflowError("subject and imageType are required")
    if not taxonomy.is_valid_subject(subject):
        raise WorkflowError(f"unknown subject: {subject!r}")
    if not taxonomy.is_valid_image_type(image_type):
        raise WorkflowError(f"unknown imageType: {image_type!r}")

    resolved = taxonomy.resolve_lora(subject, image_type)
    if not resolved or "name" not in resolved:  # pragma: no cover - both validated above
        raise WorkflowError(
            f"LoRA resolution failed for {subject!r}/{image_type!r}"
        )

    name = resolved["name"]
    if name not in allowlist():
        # resolve_lora only returns base/matrix names, so this is unreachable on
        # the happy path; fail closed if the SFW invariant is ever violated.
        raise LoraNotAllowedError(name)

    out = dict(resolved)
    out["file"] = lora_filename(name)
    return out


def resolve_and_validate_flux_lora(subject: str, image_type: str) -> Optional[dict]:
    """Backend-authoritative FLUX LoRA for a selection, or ``None`` (LoRA-free).

    The FLUX analogue of :func:`resolve_and_validate_lora`. Validates the slugs,
    resolves the OPTIONAL FLUX LoRA via :func:`app.taxonomy.resolve_flux_lora`
    (most imageTypes map to nothing → ``None`` → render on base FLUX), then — when
    a LoRA IS resolved — enforces the FLUX allowlist before returning it. A
    resolved dict carries a derived ``file`` (the ComfyUI ``lora_name`` subpath).
    Raises:

      * :class:`WorkflowError` — unknown subject or image type.
      * :class:`LoraNotAllowedError` — a resolved FLUX LoRA off the FLUX allowlist
        (unreachable via resolve_flux_lora; fails closed if the SFW invariant
        breaks — the NSFW-isolation boundary on the FLUX lane).

    Returns ``None`` for a type with no FLUX LoRA — the caller renders LoRA-free.
    """
    if not subject or not image_type:
        raise WorkflowError("subject and imageType are required")
    if not taxonomy.is_valid_subject(subject):
        raise WorkflowError(f"unknown subject: {subject!r}")
    if not taxonomy.is_valid_image_type(image_type):
        raise WorkflowError(f"unknown imageType: {image_type!r}")

    resolved = taxonomy.resolve_flux_lora(image_type)
    if not resolved:  # no FLUX LoRA for this type — render on base FLUX
        return None

    name = resolved.get("name")
    if name not in flux_allowlist():
        # resolve_flux_lora only returns FLUX_LORA names; fail closed otherwise.
        raise LoraNotAllowedError(name or "<missing>")

    out = dict(resolved)
    out["file"] = lora_filename(name)
    return out


# ── the canonical SFW SDXL txt2img template (${VAR} placeholders) ──────────
#
# ComfyUI prompt-API format. ${VAR} strings are replaced wholesale by
# _substitute (preserving the param's type). The LoRA node is wired between the
# checkpoint and the CLIP/KSampler so both the model and CLIP run through it,
# the standard SDXL LoRA placement. SaveImage terminates the graph so
# comfyui.extract_output_images() finds the outputs.
_TXT2IMG_TEMPLATE: dict[str, dict] = {
    "ckpt": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "${CKPT_NAME}"},
    },
    "lora": {
        "class_type": "LoraLoader",
        "inputs": {
            "model": ["ckpt", 0],
            "clip": ["ckpt", 1],
            "lora_name": "${LORA_NAME}",
            "strength_model": "${LORA_STRENGTH}",
            "strength_clip": "${LORA_STRENGTH}",
        },
    },
    "pos": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "${PROMPT}", "clip": ["lora", 1]},
    },
    "neg": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "${NEG_PROMPT}", "clip": ["lora", 1]},
    },
    "latent": {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": "${WIDTH}", "height": "${HEIGHT}", "batch_size": 1},
    },
    "ksampler": {
        "class_type": "KSampler",
        "inputs": {
            "model": ["lora", 0],
            "positive": ["pos", 0],
            "negative": ["neg", 0],
            "latent_image": ["latent", 0],
            "seed": "${SEED}",
            "steps": "${STEPS}",
            "cfg": "${CFG}",
            "sampler_name": "${SAMPLER}",
            "scheduler": "${SCHEDULER}",
            "denoise": 1.0,
        },
    },
    "vae": {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["ksampler", 0], "vae": ["ckpt", 2]},
    },
    "save": {
        "class_type": "SaveImage",
        "inputs": {"images": ["vae", 0], "filename_prefix": "${SAVE_PREFIX}"},
    },
}


def _substitute(value: Any, params: dict[str, Any]) -> Any:
    """Recursively replace ``${VAR}`` strings with typed values from ``params``.

    A standalone ``${VAR}`` is replaced wholesale (the param's type is preserved,
    so ``${SEED}`` becomes an int). Other strings, and non-string scalars, pass
    through. Lists/dicts recurse. Raises :class:`WorkflowError` on a missing
    placeholder (an authoring bug, surfaced loudly rather than silently sending a
    literal ``${VAR}`` to ComfyUI).
    """
    if isinstance(value, str):
        m = PLACEHOLDER_RE.match(value)
        if m:
            var = m.group(1)
            if var not in params:
                raise WorkflowError(f"missing workflow parameter: {var}")
            return params[var]
        return value
    if isinstance(value, list):
        return [_substitute(v, params) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, params) for k, v in value.items()}
    return value


def assemble_txt2img(
    *,
    subject: str,
    image_type: str,
    prompt: str,
    seed: int,
    neg_prompt: Optional[str] = None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    steps: int = DEFAULT_STEPS,
    cfg: float = DEFAULT_CFG,
    sampler: str = DEFAULT_SAMPLER,
    scheduler: str = DEFAULT_SCHEDULER,
    checkpoint: Optional[str] = None,
    lora: Optional[dict] = None,
) -> tuple[dict, dict]:
    """Build a ComfyUI txt2img graph for a SFW educational still.

    Resolves + allowlist-validates the LoRA (unless a pre-validated ``lora`` dict
    is supplied, e.g. by the job worker that already called
    :func:`resolve_and_validate_lora`), substitutes the parameters into the
    canonical SDXL template, and returns ``(workflow, meta)`` where ``meta``
    carries the resolved LoRA + effective parameters for the library row.

    The LoRA always comes from the backend (``subject`` + ``image_type``), never
    from the caller's prompt/params — the SFW injection guard. Raises
    :class:`WorkflowError` / :class:`LoraNotAllowedError` on a bad selection or
    an off-allowlist LoRA, before any graph is produced.
    """
    if not prompt or not prompt.strip():
        raise WorkflowError("prompt is required")

    # The LoRA is backend-decided. A caller-supplied dict is accepted ONLY when
    # it already passed resolve_and_validate_lora (the worker path); re-validate
    # its name regardless so a stray dict can't bypass the allowlist.
    if lora is None:
        lora = resolve_and_validate_lora(subject, image_type)
    else:
        if lora.get("name") not in allowlist():
            raise LoraNotAllowedError(lora.get("name") or "<missing>")
        lora = dict(lora)
        lora.setdefault("file", lora_filename(lora["name"]))

    params = {
        "CKPT_NAME": checkpoint or DEFAULT_CHECKPOINT,
        "LORA_NAME": lora["file"],
        "LORA_STRENGTH": float(lora.get("strength", 1.0)),
        "PROMPT": prompt,
        "NEG_PROMPT": neg_prompt if neg_prompt is not None else DEFAULT_NEG_PROMPT,
        "WIDTH": int(width),
        "HEIGHT": int(height),
        "SEED": int(seed),
        "STEPS": int(steps),
        "CFG": float(cfg),
        "SAMPLER": sampler,
        "SCHEDULER": scheduler,
        "SAVE_PREFIX": SAVE_PREFIX,
    }
    workflow = _substitute(copy.deepcopy(_TXT2IMG_TEMPLATE), params)

    meta = {
        "subject": subject,
        "image_type": image_type,
        "prompt": prompt,
        "neg_prompt": params["NEG_PROMPT"],
        "seed": params["SEED"],
        "width": params["WIDTH"],
        "height": params["HEIGHT"],
        "steps": params["STEPS"],
        "cfg": params["CFG"],
        "sampler": sampler,
        "scheduler": scheduler,
        "checkpoint": params["CKPT_NAME"],
        "lora": lora,
    }
    return workflow, meta


# ── the canonical SFW FLUX txt2img template (${VAR} placeholders) ──────────
#
# The FLUX dev-fp8 still chain, validated by a live VM100 smoke render before
# this landed. flux1-dev-fp8 is loaded via CheckpointLoaderSimple (MODEL output);
# CLIP is an explicit DualCLIPLoader(t5xxl_fp16, clip_l, type=flux); the VAE is a
# VAELoader(ae.safetensors). A FluxGuidance node sets the prompt strength (the
# KSampler cfg stays ≈1, FLUX being guidance-distilled), the latent is an
# EmptySD3LatentImage (16-channel — FLUX/SD3 latent space, NOT SDXL's
# EmptyLatentImage), and SaveImage terminates the graph so
# comfyui.extract_output_images() finds the output. Two LoRA variants are built
# at assembly time (a FLUX LoRA is OPTIONAL): with a LoRA, a LoraLoader sits
# between the loaders and the encoders/sampler (model+clip both run through it,
# the standard placement); without one, model/clip wire straight from the
# checkpoint/DualCLIPLoader. The negative is encoded but carries little weight at
# cfg≈1 — kept for parity + so the SFW negative scaffold is still present.
_FLUX_LOADERS: dict[str, dict] = {
    "ckpt": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "${CKPT_NAME}"},
    },
    "clip": {
        "class_type": "DualCLIPLoader",
        "inputs": {
            "clip_name1": "${CLIP_T5}",
            "clip_name2": "${CLIP_L}",
            "type": "flux",
        },
    },
    "fluxvae": {
        "class_type": "VAELoader",
        "inputs": {"vae_name": "${VAE_NAME}"},
    },
}
# The shared tail (encoders → guidance → sampler → decode → save). ${MODEL_SRC}
# and ${CLIP_SRC} are wired to either the LoRA node or the raw loaders by
# assemble_flux_txt2img (a node ref is a 2-list, substituted wholesale).
_FLUX_TAIL: dict[str, dict] = {
    "pos": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "${PROMPT}", "clip": "${CLIP_SRC}"},
    },
    "neg": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "${NEG_PROMPT}", "clip": "${CLIP_SRC}"},
    },
    "guidance": {
        "class_type": "FluxGuidance",
        "inputs": {"conditioning": ["pos", 0], "guidance": "${GUIDANCE}"},
    },
    "latent": {
        "class_type": "EmptySD3LatentImage",
        "inputs": {"width": "${WIDTH}", "height": "${HEIGHT}", "batch_size": 1},
    },
    "ksampler": {
        "class_type": "KSampler",
        "inputs": {
            "model": "${MODEL_SRC}",
            "positive": ["guidance", 0],
            "negative": ["neg", 0],
            "latent_image": ["latent", 0],
            "seed": "${SEED}",
            "steps": "${STEPS}",
            "cfg": "${CFG}",
            "sampler_name": "${SAMPLER}",
            "scheduler": "${SCHEDULER}",
            "denoise": 1.0,
        },
    },
    "vae": {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["ksampler", 0], "vae": ["fluxvae", 0]},
    },
    "save": {
        "class_type": "SaveImage",
        "inputs": {"images": ["vae", 0], "filename_prefix": "${SAVE_PREFIX}"},
    },
}


def assemble_flux_txt2img(
    *,
    subject: str,
    image_type: str,
    prompt: str,
    seed: int,
    neg_prompt: Optional[str] = None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    steps: int = FLUX_DEFAULT_STEPS,
    guidance: float = FLUX_DEFAULT_GUIDANCE,
    cfg: float = FLUX_DEFAULT_CFG,
    sampler: str = FLUX_DEFAULT_SAMPLER,
    scheduler: str = FLUX_DEFAULT_SCHEDULER,
    checkpoint: Optional[str] = None,
    lora: Optional[dict] = None,
) -> tuple[dict, dict]:
    """Build a ComfyUI FLUX txt2img graph for a SFW educational still.

    The FLUX-lane twin of :func:`assemble_txt2img`, mirroring its structure +
    contract: the FLUX LoRA (if any) is resolved server-side from
    ``(subject, image_type)`` via :func:`resolve_and_validate_flux_lora` and
    re-checked against the FLUX allowlist — the SFW injection guard on the FLUX
    lane. A FLUX LoRA is OPTIONAL: most imageTypes resolve to ``None`` and render
    on the base FLUX model; ``mikroskop`` / ``diagram`` / ``tvarsnitt`` route to a
    Lärbild FLUX adapter. A pre-validated ``lora`` dict (the worker path) is
    accepted but re-checked regardless; pass ``lora={}`` or a sentinel? No — pass
    the dict you got from resolve_and_validate_flux_lora (or ``None`` to let this
    resolve it, including resolving to LoRA-free).

    Returns ``(workflow, meta)``; ``meta`` carries the resolved FLUX LoRA (or
    ``None``) + effective parameters + ``model='flux'`` for the library row.

    Raises :class:`WorkflowError` / :class:`LoraNotAllowedError` on a bad
    selection or an off-allowlist FLUX LoRA, before any graph is produced.
    """
    if not prompt or not prompt.strip():
        raise WorkflowError("prompt is required")

    # Resolve the OPTIONAL FLUX LoRA backend-side, or re-validate a supplied dict.
    # `lora is None` means "resolve it" (which may itself yield None = LoRA-free).
    if lora is None:
        lora = resolve_and_validate_flux_lora(subject, image_type)
    elif lora:  # a non-empty supplied dict: re-check its name against the FLUX set
        if lora.get("name") not in flux_allowlist():
            raise LoraNotAllowedError(lora.get("name") or "<missing>")
        lora = dict(lora)
        lora.setdefault("file", lora_filename(lora["name"]))
    else:  # an explicit empty dict → render LoRA-free
        lora = None

    graph: dict[str, dict] = copy.deepcopy(_FLUX_LOADERS)
    if lora:
        # LoraLoader between the loaders and the encoders/sampler; model+clip both
        # run through it (standard placement). model_src/clip_src point at it.
        graph["lora"] = {
            "class_type": "LoraLoader",
            "inputs": {
                "model": ["ckpt", 0],
                "clip": ["clip", 0],
                "lora_name": "${LORA_NAME}",
                "strength_model": "${LORA_STRENGTH}",
                "strength_clip": "${LORA_STRENGTH}",
            },
        }
        model_src = ["lora", 0]
        clip_src = ["lora", 1]
    else:
        model_src = ["ckpt", 0]
        clip_src = ["clip", 0]

    graph.update(copy.deepcopy(_FLUX_TAIL))

    params: dict[str, Any] = {
        "CKPT_NAME": checkpoint or FLUX_CHECKPOINT,
        "CLIP_T5": FLUX_CLIP_T5,
        "CLIP_L": FLUX_CLIP_L,
        "VAE_NAME": FLUX_VAE,
        "PROMPT": prompt,
        "NEG_PROMPT": neg_prompt if neg_prompt is not None else DEFAULT_NEG_PROMPT,
        "WIDTH": int(width),
        "HEIGHT": int(height),
        "SEED": int(seed),
        "STEPS": int(steps),
        "GUIDANCE": float(guidance),
        "CFG": float(cfg),
        "SAMPLER": sampler,
        "SCHEDULER": scheduler,
        "MODEL_SRC": model_src,
        "CLIP_SRC": clip_src,
        "SAVE_PREFIX": SAVE_PREFIX,
    }
    if lora:
        params["LORA_NAME"] = lora["file"]
        params["LORA_STRENGTH"] = float(lora.get("strength", 1.0))

    workflow = _substitute(graph, params)

    meta = {
        "subject": subject,
        "image_type": image_type,
        "prompt": prompt,
        "neg_prompt": params["NEG_PROMPT"],
        "seed": params["SEED"],
        "width": params["WIDTH"],
        "height": params["HEIGHT"],
        "steps": params["STEPS"],
        "guidance": params["GUIDANCE"],
        "cfg": params["CFG"],
        "sampler": sampler,
        "scheduler": scheduler,
        "checkpoint": params["CKPT_NAME"],
        "lora": lora,
        "model": "flux",
    }
    return workflow, meta
