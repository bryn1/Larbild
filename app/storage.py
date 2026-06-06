"""The isolated, larbild-OWNED storage + SFW-only model layout (lb-83n, P0.4).

Single source of truth for WHERE larbild keeps its data on disk, plus the boot
hook that creates it. Three roots, all larbild-owned and **physically separate
from imagegen** (BUILD_PLAN hard-isolation plane #3 — separate storage / DB /
model dir; the two apps share only the ``gpu_lock`` + ``vram_pause``
coordination, never data, models, or storage):

    STORAGE_DIR/                 output media root        (env LARBILD_STORAGE)
      └── output/                generated artefacts, auto-sorted into the
            <subject>/<area>/<kind>/<file>   Bibliotek tree
    <DB_PATH>                    the SFW SQLite DB        (env LARBILD_DB_PATH)
                                 — only its PARENT dir is created here; the .db
                                 file itself is created by app.db.init_db.
    MODEL_DIR/                   the DEDICATED SFW-only model root (env
      ├── checkpoints/                                     LARBILD_MODEL_DIR)
      └── loras/

Why a model dir at all, when the app never opens a ``.safetensors``:
larbild renders via ComfyUI over HTTP — the checkpoint/LoRA *names* it sends
(:mod:`app.workflow`) are resolved by ComfyUI against ITS model dir on the GPU
host. ``MODEL_DIR`` is larbild's OWNED model root and the **deploy contract**:
in production the render host's ComfyUI checkpoint/LoRA search path points at
THIS dir, and only this dir, so a restricted imagegen/NSFW asset is never
reachable. It is also the single config knob any future model-listing code reads
— there is no other model location anywhere in ``app/``. P9.1 (lb-0ul) curates
the actual SFW base ∪ matrix set into ``checkpoints/`` + ``loras/``.

On boot, ``create_app`` calls :func:`ensure_layout` (gated by ``INIT_STORAGE``).
:func:`assert_isolated` runs first and **fails closed** if the layout is
misconfigured — the output root and the model root colliding, or any larbild
path that resolves *into* an imagegen tree (a fat-fingered
``LARBILD_MODEL_DIR=/…/imagegen-repo/models`` is exactly the accident this
catches). Refusing to boot beats silently writing SFW output into — or reading
models from — an imagegen tree. This is the path-isolation half of the model
isolation; the restricted-model-NAME half is enforced structurally elsewhere
(the backend allowlist in :mod:`app.workflow` + the CI guard
``scripts/sfw_guard.py``), so ``app/`` never duplicates a forbidden-token list.

Every accessor takes the live Flask ``config`` mapping (``app.config`` /
``current_app.config``) and derives paths on each call, so a test or a deploy
that overrides ``STORAGE_DIR`` / ``MODEL_DIR`` after the ``Config`` object is
built is always honoured (overrides are applied to ``app.config`` *after*
``Config()`` — a path precomputed in ``Config.__init__`` would be stale).
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

__all__ = [
    "StorageIsolationError",
    "storage_dir",
    "output_dir",
    "model_dir",
    "checkpoint_dir",
    "lora_dir",
    "db_parent",
    "layout",
    "assert_isolated",
    "ensure_layout",
]

# ── isolation markers ────────────────────────────────────────────────────────
# A larbild storage / DB / model path must never resolve INTO an imagegen tree.
# Checked as a case-insensitive substring of each resolved absolute path; the
# load-bearing case is a path that lands inside imagegen's checkout or its model
# dirs (a fat-fingered LARBILD_MODEL_DIR=/…/imagegen-repo/models). Deliberately
# tight (low false-positive). This is the PATH-isolation half of the model-leak
# defence; the restricted-model-NAME half is enforced structurally by the
# backend allowlist (app.workflow — baseLora ∪ loraMatrix) and the CI guard
# scripts/sfw_guard.py, so app/ never duplicates a forbidden-token denylist.
_PATH_MARKERS: tuple[str, ...] = ("imagegen", "sibbis-app", "sibbis_app")


class StorageIsolationError(RuntimeError):
    """The configured storage layout violates the SFW / imagegen isolation.

    Raised on boot (before any directory is created) when a larbild path would
    resolve into an imagegen tree, or when the output root and the model root
    collide. Fails the boot closed rather than writing into — or reading models
    from — a non-isolated location.
    """


# ── path accessors (pure; derived live from the config mapping) ───────────────
def storage_dir(config: Mapping) -> Path:
    """The output media root — ``STORAGE_DIR`` (generated artefacts live here)."""
    return Path(config["STORAGE_DIR"])


def output_dir(config: Mapping) -> Path:
    """The auto-sort output root — ``STORAGE_DIR/output``.

    The single source of truth every render/film/lesson path joins under
    (``output/<subject>/<area>/<kind>/<file>``). Not created by this accessor;
    :func:`ensure_layout` creates it on boot and the worker mkdirs it defensively.
    """
    return storage_dir(config) / "output"


def model_dir(config: Mapping) -> Path:
    """The dedicated SFW-only model root — ``MODEL_DIR`` (checkpoints/ + loras/)."""
    return Path(config["MODEL_DIR"])


def checkpoint_dir(config: Mapping) -> Path:
    """SFW base checkpoints — ``MODEL_DIR/checkpoints`` (the only checkpoint source)."""
    return model_dir(config) / "checkpoints"


def lora_dir(config: Mapping) -> Path:
    """SFW LoRAs — ``MODEL_DIR/loras`` (base ∪ matrix; the only LoRA source)."""
    return model_dir(config) / "loras"


def db_parent(config: Mapping) -> Path:
    """The directory holding the SQLite DB — ``dirname(DB_PATH)``.

    Only the parent is part of the layout; ``app.db.init_db`` creates the ``.db``.
    """
    return Path(config["DB_PATH"]).parent


def layout(config: Mapping) -> dict[str, Path]:
    """All layout dirs, keyed by role (the dirs :func:`ensure_layout` creates)."""
    return {
        "storage": storage_dir(config),
        "output": output_dir(config),
        "model": model_dir(config),
        "checkpoints": checkpoint_dir(config),
        "loras": lora_dir(config),
        "db_parent": db_parent(config),
    }


# ── isolation guard (fail-closed, runs before anything is created) ────────────
def _scan_markers(path: Path) -> str | None:
    """Return the first isolation marker found in *path*'s absolute form, else None."""
    text = str(path.resolve(strict=False)).lower()
    for marker in _PATH_MARKERS:
        if marker in text:
            return marker
    return None


def _is_nested(a: Path, b: Path) -> bool:
    """True if *a* equals *b* or one is an ancestor of the other (resolved)."""
    ra = a.resolve(strict=False)
    rb = b.resolve(strict=False)
    return ra == rb or ra in rb.parents or rb in ra.parents


def assert_isolated(config: Mapping) -> None:
    """Verify the configured layout is SFW-isolated, or raise StorageIsolationError.

    Two invariants, both fail-closed (checked before any dir is created):

      1. **No imagegen / NSFW path** — neither ``STORAGE_DIR``, ``MODEL_DIR``,
         its ``checkpoints/`` / ``loras/`` subdirs, nor the DB parent may resolve
         to a path carrying an imagegen or NSFW marker. Catches an env var
         pointed into imagegen's tree or an NSFW model dir.
      2. **Output ⟂ models** — the output media root and the model root must be
         distinct and neither nested in the other, so generated artefacts and
         model assets can never bleed into one another (the headline P0.4
         "physically separate" requirement).
    """
    paths = {
        "STORAGE_DIR": storage_dir(config),
        "MODEL_DIR": model_dir(config),
        "checkpoints": checkpoint_dir(config),
        "loras": lora_dir(config),
        "DB parent": db_parent(config),
    }
    for label, p in paths.items():
        marker = _scan_markers(p)
        if marker is not None:
            raise StorageIsolationError(
                f"{label} {str(p)!r} carries the isolation marker {marker!r} — "
                f"a larbild path must never resolve into an imagegen tree or an "
                f"NSFW model dir (SFW hard-isolation plane)."
            )

    store = storage_dir(config)
    models = model_dir(config)
    if _is_nested(store, models):
        raise StorageIsolationError(
            f"the output root {str(store)!r} and the model root {str(models)!r} "
            f"overlap — they must be distinct, non-nested trees so output media "
            f"and SFW model assets stay physically separate."
        )


def ensure_layout(config: Mapping) -> dict[str, Path]:
    """Create the full isolated layout on boot (mkdir -p, idempotent).

    Runs :func:`assert_isolated` FIRST (fail-closed) so a misconfigured path is
    refused before a single directory is written, then creates every layout dir
    if missing. Safe to call from every gunicorn worker and from tests; returns
    the resolved-role → path map it created.
    """
    assert_isolated(config)
    paths = layout(config)
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    return paths
