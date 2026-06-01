# Lärbild — SFW educational image/film studio — BUILD PLAN
*(owner-only LAN tool, 1 teacher, åk 7-9/LGR22, sibbis:6767, output fills sk2. Planned 2026-06-01. Design: Sibbis `/tmp/sfw_design/handoff/` from `sfw imaggen.zip` on backup-disk/docs.)*

## Core finding: ~70% a fork of imagegen's backend
Reuse (fork the infra modules from `/home/lektove/imagegen-repo/sibbis-app/`): GPU telemetry+routing (`api_gpus.py`), cross-host GPU lock (`gpu_lock.py` — **SHARE the same `lock:vm100-3090` key** so larbild+imagegen+chat don't OOM each other), vram_pause, Redis queue+SSE (`job_status.py`), ComfyUI client+workflow assembly+LoRA injection (`video.py`), ffmpeg mux, pnginfo embed, deploy pipeline (`deploy-sibbis.sh`→git-pull-on-Sibbis), AND **the consent-face schema (`faces`/`face_usage_log` CHECK-constraint pattern) — near-perfect template for Porträtt**.
NET-NEW: LGR22 taxonomy (production data — 17 subjects×102 areas+central, 8 imageTypes, 6 videoTypes, baseLora×8, loraMatrix×28, resolveLora — ship verbatim from `design/data.js`), subject/area auto-sort library model, Swedish TTS (Piper sv-SE), the entire React frontend, an isolated SFW-only model/LoRA set.

## Rig: NEW `larbild` rig (NOT sk2). sk2=the Skola app repo (bryn1/skola, VM500); Lärbild is a distinct Flask app on Sibbis whose OUTPUT fills Skola content. Fresh repo `bryn1/larbild`, vendor/fork imagegen modules — NEVER a branch, never merge (CI guard rejects NSFW imports).

## Phases (P0→P10), ~40 atomic beads
- **P0 Foundation** (crit): larbild repo + Flask shell (strip NSFW blueprints), SQLite+migrations, deploy script + larbild.service port 6767, isolated storage, UFW LAN-only.
- **P1 Bridge+1GPU** (crit): fork ComfyUI client+LoRA injection, queue+POST /api/jobs+SSE, first real render on vm100, reuse gpu_lock+vram_pause.
- **P2 Taxonomy+Library-read** (crit, ‖P1): ship taxonomy as prod data, resolveLora (backend-authoritative), /api/library, Bibliotek 3-level view + Centralt-innehåll panel, designsystem tokens.
- **P3 Skapa bild+autoLoRA+autosort** (crit): funnel UI, param builder+submit (server-validate LoRA), live progress→auto-sort writeback.
- **P4 2nd GPU+routing** (crit): add vm153 target, `auto` router (vram→queue-depth→prefers→OOM-fallback), telemetry pills+Kö.
- **P5 Batch**: CSV/txt parse → /api/jobs/batch fan-out.
- **P6 Film+TTS**: Piper sv-SE (sv-standard/sv-lattlast), 6 film templates+filmtype→imagetype LoRA map, storyboard→render→TTS→ffmpeg mux.
- **P7 Porträtt (consent)** [long pole]: SFW consent schema+DB CHECK floors, /api/portraits gate, under-18→guardian, audit log, face-swap only if consentOk (403 else), ReActor injection, GDPR delete.
- **P8 Lessons+Models-matrix+Kö**.
- **P9 SFW LoRA sourcing** [parallel long pole, START AT P0]: source/curate/train 8 base + 28 specific SFW LoRA into isolated dir; SFW-isolation audit.
- **P10 Polish+acceptance**: designsystem fidelity, full SFW+GDPR isolation audit, reconcile-on-boot.
Critical path: P0→P1→P3→P4→P10, gated by P9 (LoRA availability) + P7 (consent correctness).

## Hard isolation (5 planes): separate repo (no merge, CI guard); **dedicated SFW-only model dir + backend LoRA allowlist** (reject any LoRA not in baseLora∪loraMatrix → NSFW injection impossible even via crafted request); separate storage/DB/port 6767; ChromaDB 3rd isolated instance IF built (recommend DEFER semantic search in v1); SFW-only checkpoints = output safety (not prompt filtering). SHARE ONLY the gpu_lock+vram_pause coordination (mutual exclusion, not data).

## Consent/GDPR (Porträtt): DB CHECK floor `CHECK(is_minor=0 OR guardian_consent IS NOT NULL)` (physically can't persist minor w/o guardian) + API gate on create + API gate on use (server-recomputed consentOk, else 403) + ReActor only vs consented refs + audit row per use + GDPR delete cascades refs off disk + UI surfaces only consentOk.

## GPU routing — head-to-head INVERTS the design default: B70(vm153) 1.71 s/frame BEATS 3090(vm100) 2.00 → vm153 should take stills too, not just film. B70 RAM upgrade (16→32GB) raises its concurrency. Reuse imagegen's `vm153_ws=ws://192.168.5.123:8188` + `vm100_ws=ws://192.168.5.230:8188` + fallback_chain, re-tune `prefers`.

## OPEN DECISIONS (Mayor/user before filing convoy):
1. Confirm NEW larbild rig (rec: yes).
2. **SFW LoRA: source(CivitAI)/curate/train?** — the 36 LoRA don't exist yet; THE schedule driver (P9). Rec: start app on base-LoRA, fill specific cells incrementally (resolveLora falls back to base → graceful degrade).
3. TTS Piper vs XTTS (rec: Piper sv-SE, already on VM600).
4. Verify vm153 ComfyUI is actually live (Vulkan/IPEX) before P4.
5. Face-swap tech: ReActor (rec, reuse) vs InstantID.
6. Semantic search v1? (rec: defer).
7. Video model on B70 (Vulkan) vs CUDA video models → film may be vm100-pinned in v1.

## Top risks: R1 SFW-LoRA availability = schedule driver; R2 NSFW model leak (mitigate: model-dir isolation+allowlist+CI guard+audits); R3 consent UI-only=non-compliant (mitigate: DB floors+server-recompute); R4 3-way 3090 contention (SHARE gpu_lock, don't fork); R6 accidental repo merge; R7 taxonomy slug drift (single SoT JSON); R8 sk2 ingestion contract undefined (post-v1 bead).
