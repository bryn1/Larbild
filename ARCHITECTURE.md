# larbild — ARCHITECTURE

> Authored 2026-06-10 (hq-uiwe): drafted by the local vm153 35B from Mayor-gathered
> evidence, Mayor-verified and corrected. Keep current per the self-awareness mandate.


## Purpose
Lärbild is a single-teacher, LAN-only, SFW educational image and film studio for generating curriculum-aligned (åk 7-9 / LGR22) content for Skola (`sk2`). It serves as the safe, isolated sibling to `imagegen`, enforcing safety via backend model/LoRA allowlists rather than prompt filtering, and shares only GPU coordination primitives with other apps to prevent OOM on the shared 3090.

## Tech Stack
- **Backend:** Python 3 (Flask), `requirements.txt` managed via venv.
- **Frontend:** React via Babel-in-browser (no build step) — JSX + vendored React/Babel served statically by Flask (lb-tix vendored these prod-min for LAN-only operation).
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
- **Models:** No SFW model directory exists on disk — none was ever sourced. The
  `baseLora`/`loraMatrix` entries in `taxonomy.json` are PLACEHOLDER logical names
  (e.g. `photoreal-edu-v3`, `physics-vector-v3`); no matching `.safetensors` file
  exists anywhere. They are NOT dead config: `app/taxonomy.resolve_lora()` reads
  them and `app/workflow.allowlist()` derives the SFW injection-guard allowlist
  from `baseLora ∪ loraMatrix` — the security boundary the dormant render path
  enforces. KEEP them. (Diffusion-footprint audit 2026-06-23: nothing dead to
  remove; see "Diffusion footprint" below.)
- **Database:** Isolated instance (ChromaDB deferred in v1).
- **Storage:** Separate from `imagegen`. No shared data, models, or collections.
- **Telemetry:** GPU telemetry (`api_gpus.py`) and render-residency state.
- **Render queue (Redis, larbild keyspace):** `app/jobs.py` is the ONE job/queue
  mechanism. Owner-only management actions (lb-07y) live on it: `GET /api/jobs`
  (list/filter), `POST /api/jobs/<id>/cancel` (mark cancelled + LREM off the
  queue, cooperative cancel checked in `render_worker` after BRPOP + before the
  GPU submit), `POST /api/jobs/<id>/retry` (re-drive the same id). These + the Kö
  management UI in `static/js/gpu_telemetry.jsx` (`JobQueueList` in `KoModal`) are
  double-gated owner-only: `jobs_bp` is never registered on the public read-only
  factory AND the readonly guard 405s every mutating method.

## Key Directories (Post-Refactor)
- `app/`: Flask backend (forked imagegen infra modules, SFW-stripped), routers.
- `static/`: JSX screens + vendored React/Babel (Babel-in-browser, no build step).
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

## Diffusion footprint (audit 2026-06-23)
The diffusion (ComfyUI/SDXL/FLUX/LoRA) image-gen path is DEAD SCOPE as a product
direction (pivoted to authored SVG; the live library is 1068/1069 rows SVG/vector,
0 approved diffusion images). A full code+DB+import audit found **nothing genuinely
dead to remove** — every diffusion file is imported/registered/test-covered, and the
`baseLora`/`loraMatrix` config is the load-bearing SFW allowlist, not orphaned config.
Classification:
- **Dead scope, but NOT dead code:** the diffusion render path overall. Removing any
  of it would break the running private factory + ~6 tests, so it is NOT removed.
- **Functional-but-dormant (KEEP this pass):** the ComfyUI render plumbing —
  `app/comfyui.py`, `render_worker.py`, `render_lane.py`, `workflow.py`, `jobs.py`,
  `assembly.py`, `film.py`/`film_templates.py`, `batch.py`, `gpu_telemetry.py`
  (`/api/gpus`), `routes/jobs.py` (`/api/jobs`, the Kö). All owner-only (never on the
  public read-only factory). lb-dop (GPU pills/Kö) + lb-07y (cancel/retry) were built
  on it and one real FLUX render ran on VM100. Strip only on explicit owner confirm.
- **Shared / must-keep:** `taxonomy.json` + `taxonomy/__init__.py` (`resolve_lora`,
  `BASE_LORA`/`LORA_MATRIX`/`FLUX_LORA`, the allowlist), `gpu_lock.py`, `reconcile.py`.
- **The SVG pipeline is fully independent** of the render path (zero diffusion imports).
- **Residual data:** 1 live, unapproved (`review_ok=NULL`) FLUX render row remains in
  the library DB (`kemi/materia/illustration/larbild_00796_.png`, file present on disk).
  It is a legitimate pending-review item, not a dangling artifact — left for the owner
  to approve or reject via the normal review UI; NOT auto-deleted.
- **Closed as won't-do (diffusion-productionization, superseded):** lb-0ul, lb-m05,
  lb-83n, lb-xl0, lb-6ma.

## Edit Policy
- **Factual Only:** This document reflects the evidence from the provided codebase, tests, and deployment scripts.
- **Uncertainty:** Marked with `TODO-VERIFY`.
- **No Speculation:** Only stated what is explicitly shown in the rig tree, README, deploy script, and facts.
- **Last Updated:** Based on the provided snapshot of the `larbild` application.