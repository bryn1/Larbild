# larbild — ARCHITECTURE

> Authored 2026-06-10 (hq-uiwe): drafted by the local vm153 35B from Mayor-gathered
> evidence, Mayor-verified and corrected. Keep current per the self-awareness mandate.


## Purpose
Lärbild is a single-teacher, LAN-only, SFW educational image and film studio for generating curriculum-aligned (åk 7-9 / LGR22) content for Skola (`sk2`). It serves as the safe, isolated sibling to `imagegen`, enforcing safety via backend model/LoRA allowlists rather than prompt filtering, and shares only GPU coordination primitives with other apps to prevent OOM on the shared 3090.

## Tech Stack
- **Backend:** Python 3 (Flask), `requirements.txt` managed via venv.
- **Frontend:** React (built assets served statically by Flask).
- **Infrastructure:**
  - **GPU Coordination:** `gpu_lock` (mutual exclusion), `render_lane` (residency management), `render_pressure` (dispatcher signal).
  - **Queue/Status:** Redis (BRPOP loop), SSE for job status.
  - **Image Gen:** ComfyUI client, workflow assembly, LoRA injection, ffmpeg mux, pnginfo embedding.
  - **TTS:** Piper (sv-SE).
  - **Data:** ChromaDB (isolated instance, deferred in v1), SQLite/Postgres (implied by `migrations` tests and `CHECK` constraints).
- **Safety:** SFW enforced by checkpoint selection and a backend LoRA allowlist (`baseLora ∪ loraMatrix`).

## Deployment
- **Host:** Sibbis (LAN-only, UFW-gated).
- **Service:** `larbild.service` (API/UI) and `larbild-worker.service` (Render worker).
- **Port:** 6868.
- **Script:** `deploy-sibbis.sh` (runs ON Sibbis).
- **Process:**
  1. Git-pull from bare repo on VM350 (`claudecode@192.168.5.231:/home/claudecode/gt/larbild-source.git`).
  2. Idempotent check against sentinel SHA (`/home/lektove/.larbild-last-deployed-sha`).
  3. Install deps (`pip install -r requirements.txt`).
  4. Restart `larbild.service` and `larbild-worker.service`.
  5. Smoke test `/healthz` before updating sentinel.

## Runtime Topology
- **Sibbis (192.168.1.183):**
  - Runs Flask app on port **6868**.
  - Runs `larbild-worker.service` (BRPOP loop).
  - Talks to **VM100** for rendering.
- **VM100:**
  - Hosts the shared NVIDIA 3090 GPU.
  - Runs ComfyUI (render lane).
  - Coordinates via `gpu_lock` and `render_pressure` signals to avoid OOM with `imagegen` and `chat`.
- **VM350:**
  - Hosts the bare git repo (`/home/claudecode/gt/larbild-source.git`) used as the source of truth for deployments.
- **Network:** LAN-only. No public access.

## Data Stores
- **Models:** Dedicated SFW-only model directory + LoRA allowlist (`baseLora`, `loraMatrix`).
- **Database:** Isolated instance (ChromaDB deferred in v1).
- **Storage:** Separate from `imagegen`. No shared data, models, or collections.
- **Telemetry:** GPU telemetry (`api_gpus.py`) and render-residency state.

## Key Directories (Post-Refactor)
- `app/`: Flask backend (forked imagegen infra modules, SFW-stripped), routers.
- `static/`: Built React frontend assets.
- `docs/`: Architecture, build plan, design handoff notes.
- `larbild/mayor/rig/`: Rig tree (tests, data, utilities).
- `larbild/mayor/rig/tests/`: Comprehensive test suite (API, taxonomy, rendering, safety, migrations).
- `deploy-sibbis.sh`: Deployment script.

## Known Constraints/Gotchas
- **Isolation:** Must NEVER be merged with `imagegen`. CI guard rejects NSFW imports.
- **Safety:** SFW is enforced by model/LoRA allowlists, not prompt filtering.
- **GPU Sharing:** Shares mutual-exclusion coordination with `imagegen` and `chat` on VM100 3090.
- **VRAM Rule:** Ports :8091+:8092 never co-resident with ComfyUI.
- **SVG Lanes:** Two Opus-authored SVG lanes: `authored-svg` (larbild-only) vs `skola-svg-baseline` (shared with `skola.sibbamala.com`), mapped by `svg_source_path`.
- **Diffusion Images:** Deleted (junk).
- **Scoring:** Abstract-physics + generic topics score 1 on SDXL regardless of prompt (belong on procedural or FLUX).
- **Harness:** `sibbis:/home/lektove/larbild_harness.py`.

## Edit Policy
- **Factual Only:** This document reflects the evidence from the provided codebase, tests, and deployment scripts.
- **Uncertainty:** Marked with `TODO-VERIFY`.
- **No Speculation:** Only stated what is explicitly shown in the rig tree, README, deploy script, and facts.
- **Last Updated:** Based on the provided snapshot of the `larbild` application.