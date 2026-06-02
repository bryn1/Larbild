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

  /**
   * Interpret a POST /api/jobs response (lb-3gi / lb-5wp) into a funnel result.
   *
   * The endpoint returns:
   *   • 202 {job_id, status:"queued"}            → accepted, start tailing SSE.
   *   • 400 {error, valid_*}                     → a bad selection (client mirror
   *                                                 should have caught it; the
   *                                                 server is the authority).
   *   • 429 {error, depth, limit}                → the render queue is full.
   *   • 503 / other                              → transient backend trouble.
   *
   * Returns a normalised, render-ready shape:
   *   { ok: true,  jobId }
   *   { ok: false, message }   // a short Swedish sentence safe to show as-is
   *
   * Pure: takes the HTTP status + already-parsed JSON body (or null when the
   * body wasn't JSON) and never touches the network — so it is unit-testable and
   * the view's fetch wrapper stays a thin shell around it. The server's own
   * `error` text is surfaced when present (it is already specific, e.g. "unknown
   * area 'x' for subject 'fysik'"); a friendly fallback covers the rest.
   */
  function interpretSubmitResponse(status, body) {
    const b = body || {};
    if (status === 202 && b.job_id) {
      return { ok: true, jobId: b.job_id };
    }
    if (status === 429) {
      const depth = b.depth, limit = b.limit;
      const detail = (depth != null && limit != null) ? ` (${depth}/${limit} i kö)` : "";
      return { ok: false, message: `Renderkön är full — försök igen strax${detail}.` };
    }
    if (status === 400) {
      return { ok: false, message: b.error ? `Ogiltigt val: ${b.error}` : "Ogiltigt val." };
    }
    if (status === 503) {
      return { ok: false, message: b.error || "Tjänsten är tillfälligt otillgänglig." };
    }
    // any other status (incl. a 202 with no job_id — a contract slip)
    return { ok: false, message: b.error || `Kunde inte skapa jobbet (HTTP ${status}).` };
  }

  /**
   * Progress as an integer percent 0..100 from a job-state's step/total (lb-b1r).
   *
   * The render worker reports progress as ``progress_step`` / ``progress_total``
   * (e.g. diffusion step 10 of 28), NOT a fraction — so the bar computes the
   * percent here. Returns null when there is nothing meaningful to show yet
   * (queued, or total missing/zero), so the view can render an indeterminate
   * state rather than a misleading 0%. A done state is always 100. Pure.
   */
  function progressPercent(state) {
    if (!state) return null;
    if (state.status === "done") return 100;
    const step = Number(state.progress_step);
    const total = Number(state.progress_total);
    if (!Number.isFinite(step) || !Number.isFinite(total) || total <= 0) return null;
    const pct = Math.round((step / total) * 100);
    return Math.max(0, Math.min(100, pct));
  }

  // Swedish labels for the worker's status values (the SSE/job-state contract).
  const STATUS_LABELS = {
    queued: "I kö",
    running: "Renderar",
    done: "Klar",
    failed: "Misslyckades",
    cancelled: "Avbruten",
  };
  const TERMINAL_STATUSES = ["done", "failed", "cancelled"];

  /**
   * Normalise a raw job-state blob (from GET /api/jobs/<id> or the SSE stream)
   * into the fields the funnel renders (lb-b1r). Pure — no network; the view's
   * EventSource handler feeds each parsed ``data:`` payload through this.
   *
   * Input (worker contract, app/jobs.py):
   *   {status:"queued"}
   *   {status:"running", progress_step, progress_total}
   *   {status:"done",   library_id, width, height}
   *   {status:"failed", error}
   *
   * Output:
   *   { status, label, percent|null, terminal, done, failed, libraryId|null,
   *     width|null, height|null, error|null }
   */
  function describeJobState(state) {
    const s = state || {};
    const status = s.status || "queued";
    return {
      status,
      label: STATUS_LABELS[status] || status,
      percent: progressPercent(s),
      terminal: TERMINAL_STATUSES.indexOf(status) !== -1,
      done: status === "done",
      failed: status === "failed",
      libraryId: s.library_id || null,
      width: Number.isFinite(Number(s.width)) ? Number(s.width) : null,
      height: Number.isFinite(Number(s.height)) ? Number(s.height) : null,
      error: s.error || null,
    };
  }

  /**
   * The Bibliotek deep-link for a completed render's auto-sort destination
   * (lb-b1r): where the worker filed the asset, by (subject, area). The worker
   * auto-sorts the result into the library tree under exactly these slugs, so
   * the funnel can link the teacher straight to the node it landed in.
   *
   * Returns ``/bibliotek?subject=<slug>&area=<slug>`` — the existing Bibliotek
   * route, carrying the destination as query params. (The Bibliotek SPA today
   * navigates by internal state and does not yet read these params; the link
   * still lands on the library, and the params are a forward-compatible hook for
   * a future "open this node" enhancement — documented, not over-claimed.)
   * Pure URL assembly; encodes the slugs. Returns "/bibliotek" if either is
   * missing.
   */
  function bibliotekLink(subjectSlug, areaSlug) {
    if (!subjectSlug || !areaSlug) return "/bibliotek";
    const qs = `subject=${encodeURIComponent(subjectSlug)}&area=${encodeURIComponent(areaSlug)}`;
    return `/bibliotek?${qs}`;
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
    interpretSubmitResponse,
    progressPercent,
    describeJobState,
    bibliotekLink,
    STATUS_LABELS,
    TERMINAL_STATUSES,
  };
});
