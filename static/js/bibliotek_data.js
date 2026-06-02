/* ============================================================================
   Lärbild — Bibliotek data layer (lb-eee)
   ----------------------------------------------------------------------------
   Framework-free transforms over the GET /api/library response (the lb-5rc
   contract). Kept separate from the React view so the ordering + lookup logic
   is unit-testable under `node --test` with zero dependencies (the view itself
   is Babel-in-browser React and needs no build step, matching the design's
   serving model).

   The /api/library shape this consumes:
     { subjects: [ { id, name, short, hue, glyph, blurb, asset_count,
                     areas: [ { id, name, glyph, asset_count,
                                central_content: [ { id, text, asset_count } ] } ] } ],
       generated_at }

   Exposed both as a browser global (window.LBData) and as a CommonJS module
   (for the Node test), without a bundler.
   ============================================================================ */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api; // node test
  root.LBData = api; // browser
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // Priority subjects surface first in the Bibliotek grid (bead lb-eee:
  // "fysik/teknik/kemi/biologi are presented first"). Order within this list is
  // honoured; everything else keeps its taxonomy order after them.
  const PRIORITY_SUBJECTS = ["fysik", "teknik", "kemi", "biologi"];

  /**
   * Order subjects with the priority ones first (in PRIORITY_SUBJECTS order),
   * then the rest in their original taxonomy order. Pure; returns a new array,
   * never mutates the input.
   */
  function orderSubjects(subjects) {
    const list = Array.isArray(subjects) ? subjects.slice() : [];
    const rank = (s) => {
      const i = PRIORITY_SUBJECTS.indexOf(s.id);
      return i === -1 ? PRIORITY_SUBJECTS.length : i;
    };
    return list
      .map((s, i) => ({ s, i })) // keep original index for a stable tail
      .sort((a, b) => rank(a.s) - rank(b.s) || a.i - b.i)
      .map((x) => x.s);
  }

  /** Find a subject node by id, or null. */
  function findSubject(subjects, id) {
    return (subjects || []).find((s) => s.id === id) || null;
  }

  /** Find an area within a subject by id, or null. */
  function findArea(subject, areaId) {
    if (!subject) return null;
    return (subject.areas || []).find((a) => a.id === areaId) || null;
  }

  /**
   * Total objects across all subjects (the topbar/header counter). Reads the
   * pre-rolled subject asset_count values from the API; does not re-sum areas.
   */
  function totalAssets(subjects) {
    return (subjects || []).reduce((n, s) => n + (s.asset_count || 0), 0);
  }

  /**
   * The central-content sentence for an area, rejoined from its entries for the
   * panel's one-line display ("Bråk, decimaltal, …"). Mirrors how the taxonomy
   * stores it, rebuilt from the structured entries the API returns.
   */
  function centralText(area) {
    if (!area || !Array.isArray(area.central_content)) return "";
    return area.central_content.map((e) => e.text).join(", ");
  }

  /**
   * Subject color helpers — OKLCH from the subject's hue (from the API, the
   * single source of truth for per-subject hue; default = Lärbild-indigo 268).
   */
  function subjColor(subject, l = 0.55, c = 0.15) {
    const h = (subject && subject.hue != null) ? subject.hue : 268;
    return `oklch(${l} ${c} ${h})`;
  }
  function subjSoft(subject) {
    const h = (subject && subject.hue != null) ? subject.hue : 268;
    return `oklch(0.95 0.05 ${h})`;
  }

  /**
   * The Skapa-funnel link carrying the current browse context (lb-skapafix).
   *
   * The "Skapa här" / "Skapa det första" buttons hand the teacher straight into
   * the create funnel pre-scoped to where they were browsing. The Bibliotek
   * subject/area `id` values ARE the taxonomy slugs the funnel selects on (the
   * /api/library tree keys nodes by the same slug the /api/taxonomy SoT uses), so
   * they pass through verbatim as ``?subject=<id>&area=<id>``. skapa.jsx reads
   * these on bootstrap and pre-selects the funnel (falling back to its science
   * default when a param is absent/unknown).
   *
   * Pure URL assembly; url-encodes each id (defensive — slugs are simple, but
   * never inject). Returns:
   *   • "/skapa"                          when no subject is given (top-level
   *                                        "Skapa bild" — let the funnel default),
   *   • "/skapa?subject=<s>"              with a subject but no area,
   *   • "/skapa?subject=<s>&area=<a>"     with both.
   * The area is only ever included alongside a subject (areas are subject-scoped).
   */
  function skapaLink(subjectId, areaId) {
    if (!subjectId) return "/skapa";
    let qs = `subject=${encodeURIComponent(subjectId)}`;
    if (areaId) qs += `&area=${encodeURIComponent(areaId)}`;
    return `/skapa?${qs}`;
  }

  /**
   * Format a library item's ``generated_at`` for the full-size view.
   *
   * The backend stores + emits UTC as a *naive* ISO-8601 string (timezone
   * policy: store UTC, render Europe/Stockholm in the UI). A naive string is
   * parsed as local time by the JS Date, so we append a ``Z`` when no offset is
   * present to force UTC parsing, then render in Europe/Stockholm. Returns the
   * original string unchanged if it isn't parseable (never throws).
   */
  function fmtDate(iso) {
    if (!iso) return "";
    const norm = /[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z";
    const d = new Date(norm);
    if (isNaN(d.getTime())) return String(iso);
    try {
      return new Intl.DateTimeFormat("sv-SE", {
        timeZone: "Europe/Stockholm",
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit",
      }).format(d);
    } catch (_e) {
      return d.toISOString().slice(0, 16).replace("T", " ");
    }
  }

  return {
    PRIORITY_SUBJECTS,
    orderSubjects,
    findSubject,
    findArea,
    totalAssets,
    centralText,
    subjColor,
    subjSoft,
    skapaLink,
    fmtDate,
  };
});
