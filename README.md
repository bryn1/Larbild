# Lärbild

**Owner-only, SFW educational image & film studio** — the SFW sibling of `imagegen`.

Lärbild is a single-teacher LAN tool for generating curriculum-aligned (åk 7-9 / LGR22)
educational images and short films. Its output fills Skola (`sk2`) teaching content.

- **Deploys to:** Sibbis, port **6868** (LAN-only, UFW-gated)
- **Audience:** one teacher, owner-only — no public access, no multi-user
- **Priority subjects:** fysik, teknik, kemi, biologi (science first), then the rest of LGR22
- **Output safety model:** SFW is enforced by the *checkpoints + a backend LoRA allowlist*,
  not by prompt filtering. Only SFW models/LoRAs are reachable.

## Relationship to imagegen — hard isolation

Lärbild is **~70% a fork of imagegen's backend infra**, but it is a **separate repo and a
separate rig**. It is **NEVER** a branch of imagegen and must **NEVER** be merged with it.

A CI guard rejects any NSFW imports. The two apps share **only** the cross-host GPU
coordination primitives (`gpu_lock` + render-residency on the same `lock:vm100-3090` key +
the shared `vm100:render_pressure:external` dispatcher signal) so that
larbild, imagegen, and chat do not OOM each other on the shared 3090 — they share
*mutual-exclusion coordination*, never data, models, storage, or collections.

Five isolation planes:
1. Separate repo (no merge; CI guard rejects NSFW imports)
2. Dedicated SFW-only model dir + backend LoRA allowlist (reject any LoRA not in
   `baseLora ∪ loraMatrix` → NSFW injection impossible even via a crafted request)
3. Separate storage / DB / port (6868)
4. ChromaDB: a 3rd isolated instance **only if** semantic search is built (deferred in v1)
5. SFW-only checkpoints are the actual safety boundary (output safety, not prompt filtering)

## What's reused vs net-new

**Forked from imagegen** (`/home/lektove/imagegen-repo/sibbis-app/`): GPU telemetry+routing
(`api_gpus.py`), cross-host GPU lock (`gpu_lock.py`), render-residency (`render_lane.py`), Redis queue + SSE
(`job_status.py`), ComfyUI client + workflow assembly + LoRA injection (`video.py`), ffmpeg
mux, pnginfo embed, deploy pipeline (`deploy-sibbis.sh` → git-pull-on-Sibbis), and the
consent-face schema (`faces` / `face_usage_log` CHECK-constraint pattern) as the Porträtt
template.

**Net-new:** LGR22 taxonomy (17 subjects × 102 areas + central content, 8 imageTypes,
6 videoTypes, baseLora × 8, loraMatrix × 28, `resolveLora` — shipped verbatim as production
data from the design's `data.js`), subject/area auto-sort library model, Swedish TTS
(Piper sv-SE), the entire React frontend, and an isolated SFW-only model/LoRA set.

## Repo layout

```
app/        Flask backend (forked imagegen infra modules, SFW-stripped) + routers
static/     Built React frontend assets served by Flask
docs/       Architecture, build plan, design handoff notes
```

## System dependencies (Sibbis deploy)

Python deps install from `requirements.txt` (`pip install -r requirements.txt`).
Two things ALSO need OS packages on the deploy host:

```bash
sudo apt-get install -y graphviz ffmpeg
```

- **graphviz** — the `dot` binary backs the procedural block/flow/concept/cycle
  diagram renderer (`app/procedural.py`). Without it the engine auto-falls back
  to a matplotlib box+arrow renderer (diagrams still render), so install it for
  the cleaner graphviz routing. The Python `graphviz` wrapper is in
  `requirements.txt`; it needs this binary.
- **ffmpeg** — the film/lesson mux step.

The procedural diagram engine (structured imageTypes: `diagram` / `schema` /
`tidslinje` / `molekyl` / `reaction`) is **CPU-only** — graphviz + matplotlib +
RDKit draw the structure with real Swedish labels, no GPU. It renders even while
the VM100 3090 is busy with diffusion. RDKit ships as a pure pip wheel (no apt).

## Build plan & design

- Build plan: `docs/BUILD_PLAN.md` (phases P0–P10, ~40 beads, acceptance criteria)
- SFW design handoff: Sibbis `/tmp/sfw_design/handoff/` (from `sfw imaggen.zip` on
  `backup-disk/docs`) — taxonomy lives in `design/data.js`

## Status

Bootstrap skeleton only. Application code is built by polecats per the phase beads
(prefix `lb-`). See `docs/BUILD_PLAN.md`.
