# Lärbild

**Owner-only, SFW educational image library** — the SFW sibling of `imagegen`.

Lärbild is a single-teacher LAN tool for browsing + reviewing curriculum-aligned
(åk 7-9 / LGR22) educational images. Its content fills Skola (`sk2`) teaching
material. The library is **authored SVG / vector content** — hand-authored
concept diagrams imported and rasterised, not model-generated.

- **Deploys to:** Sibbis, port **6868** (LAN-only, UFW-gated)
- **Public mirror:** bilder.sibbamala.com (read-only, approved images only)
- **Audience:** one teacher, owner-only on the LAN tool — no multi-user
- **Priority subjects:** fysik, teknik, kemi, biologi (science first), then the rest of LGR22

## Authored-SVG only (diffusion render path removed)

Lärbild originally carried a diffusion (ComfyUI/SDXL/FLUX/LoRA) render path forked
from imagegen. That path was **removed** when the project committed permanently to
authored SVG content: the ComfyUI client, the Redis render-job queue, the GPU
lock / render-residency, the procedural diagram engine, the film/lesson render
funnels, the Porträtt face stack, TTS, the LoRA matrix, and the GPU telemetry /
Kö UI are all gone. The library is now populated by importing authored SVGs (see
`scripts/import_skola_svgs.py`), rasterised to PNG with cairosvg.

## Relationship to imagegen — hard isolation

Lärbild is a **separate repo and a separate rig**. It is **NEVER** a branch of
imagegen and must **NEVER** be merged with it. A CI guard rejects any NSFW
imports. With the render path gone, larbild no longer shares any GPU coordination
with imagegen — it is a CPU-only Flask app that serves a library of imported SVG
assets.

## What it does

- **Browse:** the 3-level Bibliotek (ämne → område → bilder) over the LGR22
  taxonomy (17 subjects × 102 areas + Skolverket central content).
- **Review:** the owner marks each asset OK / Inte OK with a note
  (`POST /api/library/<id>/review`).
- **Flag:** visitors on the public mirror can report a wrong/broken image
  (`POST /api/flag`, the single sanctioned public write).
- **Lessons:** group existing library assets into named, ordered lessons
  (asset-lessons, `/api/asset-lessons*`).

## Repo layout

```
app/        Flask backend (taxonomy, library, media, review, flags, asset-lessons) + routers
static/     no-build React frontend (Babel-in-browser) + vendored React/Babel
scripts/    authored-SVG import pipeline (import_skola_svgs.py, build_svg_mapping.py)
docs/       Architecture, build plan, design handoff notes
```

## System dependencies (Sibbis deploy)

Python deps install from `requirements.txt` (`pip install -r requirements.txt`).
One thing ALSO needs an OS package on the deploy host:

```bash
sudo apt-get install -y libcairo2   # cairosvg needs the system libcairo
```

- **libcairo** — backs `cairosvg`, which rasterises the imported authored SVGs to
  white-background library PNGs on import (`scripts/import_skola_svgs.py`). CPU-only.

## Deployment

`deploy-sibbis.sh` runs ON Sibbis: git-pulls the latest master into the live
checkout, reinstalls deps, and restarts `larbild.service` (and removes the
retired `larbild-worker.service` if a prior deploy installed it). The public
read-only instance runs as `larbild-public.service`.

## Build plan & design

- Build plan: `docs/BUILD_PLAN.md`
- Architecture: `ARCHITECTURE.md`
