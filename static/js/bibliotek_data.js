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

  return {
    PRIORITY_SUBJECTS,
    orderSubjects,
    findSubject,
    findArea,
    totalAssets,
    centralText,
    subjColor,
    subjSoft,
  };
});
