# larbild — ARCHITECTURE

> Authored 2026-06-10 (hq-uiwe): drafted by the local vm153 35B from Mayor-gathered
> evidence, Mayor-verified and corrected. Keep current per the self-awareness mandate.


## Purpose
Lärbild is a single-teacher, LAN-only, SFW educational **image library** for
curriculum-aligned (åk 7-9 / LGR22) content for Skola (`sk2`). The library is
**authored SVG / vector content** — hand-authored concept diagrams imported and
rasterised to PNG — not model-generated. It serves a 3-level browse (Bibliotek),
an owner review workflow, a visitor-flag path, and asset-lesson grouping, plus a
public read-only mirror at bilder.sibbamala.com.

## Tech Stack
- **Backend:** Python 3 (Flask), `requirements.txt` managed via venv. CPU-only.
- **Frontend:** React via Babel-in-browser (no build step) — JSX + vendored React/Babel served statically by Flask (lb-tix vendored these prod-min for LAN-only operation).
- **Authored-SVG pipeline:** `scripts/import_skola_svgs.py` imports hand-authored
  concept SVGs, sanitises them in-memory, and rasterises to white-bg PNGs with
  `cairosvg`; `app/svg_baseline.py` maps SVGs to LGR22 concepts.
- **Data:** SQLite (`app/db.py` + `app/migrations/`). The library row carries SVG
  provenance (`source`, `svg_source_path`, `preferred`; migration 0007).
- **Safety:** the public mirror is read-only (approved-only + a before_request
  guard); the LAN tool is owner-only.

## Diffusion render path — REMOVED (2026-06-23, owner-confirmed)
The diffusion (ComfyUI/SDXL/FLUX/LoRA) image-gen path was a full fork of
imagegen's backend. It is **gone**: the owner confirmed larbild is permanently
authored-SVG only (the live library is 1068/1069 rows SVG/vector, 0 approved
diffusion images). Removed in the full strip:
- **App modules:** `comfyui.py`, `render_worker.py`, `render_lane.py`,
  `workflow.py`, `jobs.py`, `assembly.py`, `batch.py`, `film.py`,
  `film_templates.py`, `gpu_telemetry.py`, `gpu_lock.py`, `mux.py`, `caption.py`,
  `portraits.py`, `procedural.py`, `edu_prompt.py`, `lesson.py`, `tts.py`.
- **Routes:** `routes/jobs.py`, `batch.py`, `films.py`, `lessons.py`, `lora.py`,
  `portraits.py`, `tts.py` (endpoints `/api/jobs`, `/api/gpus`, `/api/films`,
  `/api/lessons`, `/api/tts`, `/api/resolve-lora`, the Kö — all 404 now).
- **Frontend:** `gpu_telemetry.jsx` (GPU pills + Kö), `skapa.jsx`/`skapa_data.js`
  (the generate funnel), `skapa.html`; the regenerate UI, GPU pills, Kö modal,
  job-event SSE, and "Skapa" buttons were excised from `bibliotek.jsx`.
- **Config:** the `baseLora`/`loraMatrix` data + `resolve_lora`/`resolve_flux_lora`/
  `flux_lora_names`/`BASE_LORA`/`LORA_MATRIX`/`FLUX_LORA` (the SFW LoRA allowlist)
  were removed from `taxonomy.json` + `taxonomy/__init__.py` — they only ever fed
  the render path's SFW injection guard, which no longer exists.
- **Route:** `POST /api/library/<id>/regenerate` (it enqueued a render job).
- **Service:** `larbild-worker.service` + its redis drop-in (deploy-sibbis.sh now
  removes them if a prior deploy installed them).
- **Deps:** redis, requests, flask-sock, simple-websocket, matplotlib, graphviz,
  rdkit, numpy dropped from `requirements.txt`.

**Residual data (NOT removed):** 1 live, unapproved (`review_ok=NULL`) FLUX render
row remains in the library DB (`kemi/materia/illustration/larbild_00796_.png`) — a
legitimate pending-review item for the owner to approve/reject via the normal
review UI. The library schema's render-provenance columns (`lora`, `seed`,
`backend`, `workflow_json`, `parent_id`, the `jobs`/`batches`/`lessons`/`faces`
tables) are left in place (harmless; would just be NULL/empty on SVG rows).

## Deployment
- **Host:** Sibbis (LAN-only, UFW-gated).
- **Service:** `larbild.service` (the LAN tool, :6868) and `larbild-public.service`
  (the read-only public mirror, bilder.sibbamala.com).
- **Port:** 6868.
- **Script:** `deploy-sibbis.sh` (runs ON Sibbis).
- **Process:**
  1. Git-pull from bare repo on VM350 (`claudecode@192.168.5.231:/home/claudecode/gt/larbild-source.git`).
  2. Idempotent check against sentinel SHA (`/home/lektove/.larbild-last-deployed-sha`).
  3. Install deps (`pip install -r requirements.txt`).
  4. Remove the retired `larbild-worker.service` + redis drop-in if present; restart `larbild.service`.
  5. Smoke test `/healthz` before updating sentinel.

## Runtime Topology
- **Sibbis (192.168.1.183):** runs the Flask app on port **6868** (and the public
  read-only instance). CPU-only — no GPU host involved.
- **VM350:** hosts the bare git repo (`/home/claudecode/gt/larbild-source.git`),
  the source of truth for deployments.
- **Network:** LAN-only for the tool; the public mirror is fronted by the owner's
  Cloudflare tunnel (SAME tunnel that serves skola — NEVER touch cloudflared).

## Data Stores
- **Database:** SQLite (`data/larbild.db`), isolated from imagegen.
- **Storage:** Separate from `imagegen`. No shared data, models, or collections.

## Key Directories
- `app/`: Flask backend (taxonomy, library, media, review, flags, asset-lessons) + routers.
- `static/`: JSX screens + vendored React/Babel (Babel-in-browser, no build step).
- `scripts/`: the authored-SVG import pipeline + mapping builder.
- `docs/`: Architecture, build plan, design handoff notes.
- `tests/`: API, taxonomy, SVG-baseline, review, flags, asset-lessons, migrations, reconcile.
- `deploy-sibbis.sh`: Deployment script.

## Known Constraints/Gotchas
- **Isolation:** Must NEVER be merged with `imagegen`. CI guard rejects NSFW imports.
- **SVG Lanes:** Two Opus-authored SVG lanes: `authored-svg` (larbild-only) vs `skola-svg-baseline` (shared with `skola.sibbamala.com`), mapped by `svg_source_path`.
- **Public mirror:** read-only; approved-only (`review_ok=1`) + a before_request guard 405s every mutating method except the single sanctioned `POST /api/flag`.
- **No GPU:** larbild is CPU-only since the render-path strip — it no longer coordinates with imagegen/chat on the 3090.

## Edit Policy
- **Factual Only:** This document reflects the evidence from the provided codebase, tests, and deployment scripts.
- **Uncertainty:** Marked with `TODO-VERIFY`.
- **No Speculation:** Only stated what is explicitly shown in the rig tree, README, deploy script, and facts.
- **Last Updated:** Based on the provided snapshot of the `larbild` application.