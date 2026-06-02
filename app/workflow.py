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
    "resolve_and_validate_lora",
    "assemble_txt2img",
    "lora_filename",
]

PLACEHOLDER_RE = re.compile(r"^\$\{([A-Z0-9_]+)\}$")

# Defaults; the actual checkpoint file is sourced in P9. Until it exists, the
# graph is still assemblable + testable — only the *live* render is deferred.
DEFAULT_CHECKPOINT = os.environ.get("LARBILD_SDXL_CHECKPOINT", "sdxl-edu-base.safetensors")
LORA_SUFFIX = os.environ.get("LARBILD_LORA_SUFFIX", ".safetensors")

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
# A neutral SFW filename prefix for SaveImage so outputs sort under larbild/.
SAVE_PREFIX = "larbild"


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
