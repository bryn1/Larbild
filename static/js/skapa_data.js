/* ============================================================================
   Lärbild — Skapa (create-image funnel) data layer (lb-e1p / P3.1)
   ----------------------------------------------------------------------------
   Framework-free transforms behind the "Skapa bild" funnel, kept separate from
   the React view (static/js/skapa.jsx) so the funnel logic is unit-testable
   under `node --test` with zero dependencies — same no-build, Babel-in-browser
   serving model as the Bibliotek view (bibliotek_data.js / lb-eee).

   The funnel walks  ämne (subject) → område (area) → bildtyp (imageType) → params,
   driven entirely by the taxonomy SoT delivered by GET /api/taxonomy (lb-582).
   The shape this module consumes is that endpoint's `as_dict()` output:

     { subjects:  [ { slug, name, short, hue, glyph, blurb,
                      areas: [ { slug, name, glyph, central } ] } ],
       imageTypes:[ { slug, name, glyph, desc } ],
       videoTypes, narrationVoices, baseLora, loraMatrix }

   This layer never resolves the LoRA itself — the backend is authoritative
   (POST /api/resolve-lora, lb-9tz). It only:
     • orders/locates taxonomy nodes (science subjects first),
     • reports whether the current selection is ready to submit, and
     • builds the EXACT job payload POSTed to /api/jobs (lb-3gi) — deliberately
       WITHOUT a `lora` field, because the worker resolves + allowlist-validates
       the LoRA server-side (the SFW injection guard). A client LoRA is ignored
       by the backend anyway; we never even send one.

   Exposed both as a browser global (window.SkapaData) and a CommonJS module
   (for the Node test), without a bundler.
   SFW-only: pure taxonomy/funnel logic; no restricted import, model, or feature.
   ============================================================================ */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api; // node test
  root.SkapaData = api; // browser
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // Science subjects surface first in the funnel's subject picker (BUILD_PLAN:
  // "fysik/teknik/kemi/biologi surface first"). Same contract + order as the
  // Bibliotek grid. Everything else keeps its taxonomy order after them.
  const PRIORITY_SUBJECTS = ["fysik", "teknik", "kemi", "biologi"];

  // Default accent hue (Lärbild-indigo) when no subject hue is known — matches
  // the design token --brand-h default (268).
  const DEFAULT_HUE = 268;

  /**
   * Order subjects with the priority (science) ones first, in PRIORITY_SUBJECTS
   * order, then the rest in their original taxonomy order. Pure; returns a new
   * array, never mutates the input. Mirrors bibliotek_data.orderSubjects so the
   * two views present subjects identically.
   */
  function orderSubjects(subjects) {
    const list = Array.isArray(subjects) ? subjects.slice() : [];
    const rank = (s) => {
      const i = PRIORITY_SUBJECTS.indexOf(s && s.slug);
      return i === -1 ? PRIORITY_SUBJECTS.length : i;
    };
    return list
      .map((s, i) => ({ s, i })) // keep original index for a stable tail
      .sort((a, b) => rank(a.s) - rank(b.s) || a.i - b.i)
      .map((x) => x.s);
  }

  /** Find a subject node by slug, or null. */
  function findSubject(subjects, slug) {
    return (subjects || []).find((s) => s.slug === slug) || null;
  }

  /** Find an area within a subject by slug, or null (areas are subject-scoped). */
  function findArea(subject, areaSlug) {
    if (!subject) return null;
    return (subject.areas || []).find((a) => a.slug === areaSlug) || null;
  }

  /** Find an image type by slug, or null. */
  function findImageType(imageTypes, slug) {
    return (imageTypes || []).find((t) => t.slug === slug) || null;
  }

  /**
   * The accent hue for a subject (the per-view --brand-h). Falls back to the
   * Lärbild-indigo default when the subject or its hue is missing.
   */
  function subjectHue(subject) {
    return subject && subject.hue != null ? subject.hue : DEFAULT_HUE;
  }

  /** Subject colour (OKLCH) from its hue — for the context dot, mirrors bibliotek. */
  function subjColor(subject, l = 0.55, c = 0.15) {
    return `oklch(${l} ${c} ${subjectHue(subject)})`;
  }

  /**
   * The auto-sort destination label for the current selection, e.g.
   * "Fysik › Ljud". When no area is chosen yet, the funnel can't enqueue (area
   * is required), so we show the prompt-to-pick form. Pure string assembly over
   * the resolved subject/area nodes; the funnel passes the looked-up objects.
   */
  function destinationLabel(subject, area) {
    const subjName = subject ? subject.name : "(välj ämne)";
    if (!area) return `${subjName} › (välj område)`;
    return `${subjName} › ${area.name}`;
  }

  /**
   * Is the current selection complete enough to submit a render?
   * The backend (POST /api/jobs, lb-3gi) requires, for kind=bild: a valid
   * subject, a valid (subject-scoped) area, a valid imageType, and a non-empty
   * prompt. This is the client-side mirror so the Generera button can be
   * disabled with a precise reason BEFORE a round-trip — the server stays the
   * authority and re-validates everything.
   *
   * Returns { ok, reason } where reason is a short Swedish hint (or "" when ok).
   * Validity is checked against the live taxonomy nodes the caller passes, not a
   * hardcoded list, so it can never drift from the SoT.
   */
  function submitReadiness({ subject, area, imageType, prompt }) {
    if (!subject) return { ok: false, reason: "välj ämne" };
    if (!area) return { ok: false, reason: "välj område" };
    if (!imageType) return { ok: false, reason: "välj bildtyp" };
    if (!prompt || !prompt.trim()) return { ok: false, reason: "skriv en beskrivning" };
    return { ok: true, reason: "" };
  }

  /**
   * Build the EXACT JSON body POSTed to /api/jobs (lb-3gi) for a single image
   * render. The funnel passes slugs (not nodes) plus the optional params.
   *
   * Contract honoured verbatim from the jobs endpoint:
   *   • kind defaults to "bild".
   *   • subject / area / imageType are sent as their slugs (validated server-side
   *     against the taxonomy SoT — an invalid pair is rejected 4xx).
   *   • prompt is required (the endpoint 400s on empty); trimmed here.
   *   • seed is optional; only included when a finite integer is given (the
   *     endpoint 400s on a non-int, so we never send a blank/NaN).
   *   • count is optional UI sugar (how many images the teacher wants); it rides
   *     in params for provenance — the v1 worker renders one per job.
   *   • NO `lora` / `loraName` is ever included. The worker resolves the LoRA
   *     from (subject, imageType) and allowlist-validates it (the SFW guard);
   *     the endpoint ignores a client LoRA regardless. Not sending one makes the
   *     "backend is authoritative" contract impossible to get wrong here.
   *
   * Pure: returns a plain object, reads nothing global. Unknown extras in
   * `params` are passed through under `params` (the endpoint stores the params
   * blob for provenance, minus any lora key).
   */
  function buildJobPayload({ subject, area, imageType, prompt, seed, title, count, params }) {
    const body = {
      kind: "bild",
      subject: subject,
      area: area,
      imageType: imageType,
      prompt: (prompt || "").trim(),
    };
    if (title && title.trim()) body.title = title.trim();

    const s = normalizeSeed(seed);
    if (s !== null) body.seed = s;

    // Optional render params for provenance. count is clamped to a sane 1..8 so
    // a stray value can't ask for a runaway batch; the worker still renders one
    // image per job in v1. Never carries a lora key.
    const extra = Object.assign({}, params || {});
    delete extra.lora;
    delete extra.loraName;
    const c = clampCount(count);
    if (c !== null) extra.count = c;
    if (Object.keys(extra).length) body.params = extra;

    return body;
  }

  /**
   * Coerce a seed to a non-negative integer, or null when absent/blank/invalid.
   * The funnel's seed field is a free text/number input; an empty field means
   * "let the worker pick", so we omit it rather than send 0/NaN. A negative or
   * non-finite value is treated as absent.
   */
  function normalizeSeed(seed) {
    if (seed === null || seed === undefined || seed === "") return null;
    const n = Number(seed);
    if (!Number.isFinite(n)) return null;
    const i = Math.trunc(n);
    return i >= 0 ? i : null;
  }

  /** Clamp a requested image count to 1..8, or null when absent/invalid (→ default 1). */
  function clampCount(count) {
    if (count === null || count === undefined || count === "") return null;
    const n = Number(count);
    if (!Number.isFinite(n)) return null;
    return Math.max(1, Math.min(8, Math.trunc(n)));
  }

  return {
    PRIORITY_SUBJECTS,
    DEFAULT_HUE,
    orderSubjects,
    findSubject,
    findArea,
    findImageType,
    subjectHue,
    subjColor,
    destinationLabel,
    submitReadiness,
    buildJobPayload,
    normalizeSeed,
    clampCount,
  };
});
