# Lärbild image-library audit — fysik begrepp diagrams (2026-06-10)

> Owner ask: full audit of the Opus-authored images in the lb rig + build a complete library —
> ONE diagram per ugglansno begrepp, per area, for all of fysik (later biologi/kemi/teknik).
> These diagrams are destined for the Skola (sk2) platform.
>
> Method: 4 parallel max-effort Fable agents VISUALLY opened all **470 live SVG diagrams** (every
> rendered PNG, plus SVG-source verification of every suspect arrow/count) + a coverage analysis
> vs the 441 ugglansno fysik begrepp in `edu_data/fysik.json`. Mayor ground-truthed samples and
> applied the curation. (The 1250 old diffusion images were already purged — not in scope.)

## Headline
- **Verdicts across 470 diagrams: KEEP 267 · PROMOTE 154 · FIX 46 · REJECT 3.** Quality **4/5** —
  the authored-svg fysik sets are near-textbook; physics was verified correct nearly everywhere.
- **The library was far healthier than the accept-count implied:** 154 good diagrams sat unreviewed
  or over-rejected (the prior review pass was notably over-strict on ljud + atom re-renders).
- **APPLIED (DB backed up `larbild.db.bak-audit-2026-06-10`):** promoted 154 → accepted, demoted 2.
  New state: **accepted 440 · rejected 9 · unreviewed 21.**
- **Begrepp coverage: 65% → 92% (407/441).** 34 begrepp still have no accepted image (the
  generation backlog).
- **2 demoted accepts taught real misconceptions** (the key catch): *Motstånd* ("current is used
  up" — equal current shown unequal across a series resistor) and *Medium* (light bending away from
  the normal into denser media). Both need a redraw before sk2 use.

## Systemic render-pipeline bugs (fix once — they recur and will hit future generation)
1. **Glyph tofu:** emoji (❄ U+2744) and superscripts (⁺/⁻ U+207A/B) render as empty □ boxes — the
   render font lacks them. Hits Stelning, Kondensera, Isotop, Betapartiklar… Fix: embed a font with
   these, or draw them as paths / strip-and-substitute at render time.
2. **Circular-process connectors:** cycle diagrams (Teknikutvecklingsprocessen, Livscykelanalys)
   draw connectors as plain lines that stab through node circles + labels with no arrowheads → cycle
   direction unreadable.
3. **Markup-escaping bug:** literal `<tspan …>` leaked as visible text (Lokalnät).
4. **Authoring pattern (not a render bug):** current/field **direction arrows** are the #1
   correctness risk (Elmotor, Magnetfält, Ohms lag, Motstånd, Medium all had a wrong arrow). Add a
   direction check to the generation gate.

## Coverage — the build-to-100% backlog (34 missing begrepp)
universum 10 · varme-klimat 8 · atom 5 · el-magnet 4 · ljus 4 · grundfysik 2 · rorelse 1.
Full list in `/tmp/lb_gaps_final.json` (to be folded into the generation bead). Reaching 100%
fysik coverage = generate these 34 + redraw the 3 REJECTs + fix the ~28 excluded defects.

## The begrepp-spine unification (important)
`edu_data/fysik.json` (441 begrepp: term + förklaring + vardagligt_exempel + area + suggested
image type) is the **same authoritative ugglansno concept list** that should seed the sk2 question
bank's `concept_registry` (spec sk2-pb3). One begrepp spine drives BOTH the questions and the
diagrams, and clones the same way to biologi/kemi/teknik. Wire `edu_data/fysik.json` in as the
registry seed rather than maintaining two lists.

## Per-cluster detail (agent reports)
- **A grundfysik+rörelse (111):** KEEP 94 / PROMOTE 10 / FIX 7 / REJECT 0. Flawed accepts: Komprimera
  (particle count 8→11 not conserved), Hävstång (double-headed arrow) + 4 artifact/legibility. Sluten
  krets stays rejected (current arrows vs battery polarity).
- **B varme-klimat+atom (114):** KEEP 32 / PROMOTE 70 / FIX 12 / REJECT 0. Flawed accepts:
  Partikelstrålning (gamma mislabeled as particle + passes through bly), Isotop/Jon (tofu + drawn
  count ≠ stated). Lokalnät markup leak; Fjärrvärme loop not closed.
- **C universum+ljud+teknik (131):** KEEP 64 / PROMOTE 48 / FIX 18 / REJECT 1 (Fixstjärna — concept
  wrong). Flawed accepts: Solförmörkelse (no umbra cone), Mikroorganism (unrecognizable microscope).
  Mis-shelved: Oxidera (kemi), Extremofil/Mikroorganism (biologi), Megalitgrav (historia),
  Graviton/Cepheid/Epicykel (fördjupning) — re-tag.
- **D el-magnet+ljus (114):** KEEP 77 / PROMOTE 26 / FIX 9 / REJECT 2 (Motstånd, Medium — demoted).
  6 arrow-direction fixes (Elmotor, Magnetfält, Ohms lag, Kortslutning, Ström, Likström). ljus
  ray-optics near-flawless (Ljusbrytning n=sin45/sin28=1.5 verified).
