# Lärbild storage & model layout (P0.4, lb-83n)

Lärbild keeps **all** of its on-disk state in three larbild-OWNED roots, each
config-driven and created on boot. They are **physically separate from
imagegen** — the BUILD_PLAN's hard-isolation plane #3 (*separate storage / DB /
model dir*). The two apps share **only** the cross-host GPU coordination
(`gpu_lock` + `vram_pause`, the `lock:vm100-3090` key) so they don't OOM each
other on the shared 3090 — they share *mutual-exclusion coordination, never
data, models, or storage*.

The single source of truth in code is [`app/storage.py`](../app/storage.py); the
knobs are defined in [`app/config.py`](../app/config.py).

## The three roots

```
STORAGE_DIR/                     output media root        env LARBILD_STORAGE
  └── output/                    generated artefacts, auto-sorted:
        <subject>/<area>/<kind>/<file>     ← the Bibliotek tree
        (e.g. output/fysik/rorelse/diagram/larbild_00001_.png)

<DB_PATH>                        the SFW SQLite database   env LARBILD_DB_PATH
  (its PARENT dir is part of the layout; the .db file is created by
   app.db.init_db, the schema by app/migrations/)

MODEL_DIR/                       the DEDICATED SFW-only model root
  ├── checkpoints/               SFW base checkpoints      env LARBILD_MODEL_DIR
  └── loras/                     SFW LoRAs (base ∪ matrix)
```

`STORAGE_DIR/portraits/` (Porträtt consent reference images) is **not** part of
the boot layout — it is created lazily by the portraits route only when the
first consented face is registered, so a fresh boot leaves it absent.

## Config knobs (env-overridable, safe dev defaults)

| Env var | Config attr | Default (dev) | Holds |
|---|---|---|---|
| `LARBILD_STORAGE` | `STORAGE_DIR` | `<repo>/storage` | output media (`output/…`) |
| `LARBILD_DB_PATH` | `DB_PATH` | `<repo>/data/larbild.db` | the SQLite DB |
| `LARBILD_MODEL_DIR` | `MODEL_DIR` | `<repo>/models` | `checkpoints/` + `loras/` |
| `LARBILD_INIT_STORAGE` | `INIT_STORAGE` | `1` (on) | create the layout on boot |

Defaults live under the repo root so a dev checkout runs out of the box. The
Sibbis deploy (`/home/lektove/larbild-app`) overrides them via the systemd unit
to the production locations. The defaults — and any production override — sit in
larbild's own tree, never inside imagegen's (`/home/lektove/imagegen-repo/…`).

## Created on boot

`create_app` → `_init_storage` → `app.storage.ensure_layout(app.config)` (gated
by `INIT_STORAGE`, default on, independent of `INIT_DB`):

1. **`assert_isolated` runs first, fail-closed.** It refuses to boot
   (`StorageIsolationError`) if any root would resolve into an imagegen tree or
   an NSFW model dir (e.g. a fat-fingered `LARBILD_MODEL_DIR=…/imagegen-repo/models`),
   or if the output root and the model root collide. Refusing to boot beats
   silently writing SFW output into — or reading models from — an imagegen tree.
2. **`mkdir -p`** for `STORAGE_DIR`, `STORAGE_DIR/output`, `MODEL_DIR`,
   `MODEL_DIR/checkpoints`, `MODEL_DIR/loras`, and the DB parent. Idempotent and
   safe to run from every gunicorn worker.

Paths are derived live from `app.config` on each call, so a test/deploy that
overrides `STORAGE_DIR`/`MODEL_DIR` after `Config()` is built is honoured.

## Why a model dir, when the app never opens a `.safetensors`

Lärbild renders via **ComfyUI over HTTP** ([`app/comfyui.py`](../app/comfyui.py)):
the checkpoint/LoRA *names* the workflow sends
([`app/workflow.py`](../app/workflow.py)) are resolved by ComfyUI against **its**
model dir on the GPU host (VM100 / vm153). The larbild Flask app itself never
reads a model file.

So `MODEL_DIR` is two things:

- **larbild's owned model root** — the only checkpoint/LoRA location the app
  knows about (there is no other model path anywhere in `app/`), and the single
  config knob any future model-listing/Models-matrix code (P8) reads.
- **the deploy contract** — in production the render host's ComfyUI
  checkpoint/LoRA search path points at **this dir, and only this dir** (via
  ComfyUI's `extra_model_paths.yaml`), so imagegen's restricted assets — which
  live in a *different* dir on the same box — are never reachable by a larbild
  render. FLUX LoRAs are additionally namespaced under a `larbild/` subpath in
  their ComfyUI name (see the taxonomy), keeping them out of imagegen's
  `loras/bondage/`, `loras/faces/`, … even within a shared loras tree.

This dir-isolation is the **first** of the model-leak mitigations (BUILD_PLAN
R2: *model-dir isolation + allowlist + CI guard + audits*). The **second**, the
load-bearing runtime guard, is the backend LoRA allowlist in
[`app/workflow.py`](../app/workflow.py): every render resolves a LoRA only from
`baseLora ∪ loraMatrix` and rejects anything else — so even a crafted request
can't inject an off-allowlist model. The **third** is the CI guard
[`scripts/sfw_guard.py`](../scripts/sfw_guard.py).

## What populates it

P9.1 (`lb-0ul`, blocked on this bead) sources/curates the 8 base SFW LoRAs (and
later the matrix cells + base checkpoint) into `MODEL_DIR/loras` +
`MODEL_DIR/checkpoints`. Until then the dirs exist but are empty and live renders
are deferred — the workflow still assembles + unit-tests against the names.

## Never committed

`/models/`, `/storage/`, `/data/`, `/output/`, `/results/` are git-ignored
(see [`.gitignore`](../.gitignore)) — runtime data and large model assets, never
source.
