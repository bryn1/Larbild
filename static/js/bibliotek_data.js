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

  /* ----------------------------------------------------------------------
   * URL <-> browse-location (lb-review-regen: refresh/navigation fix)
   * --------------------------------------------------------------------- */

  /**
   * Read the current Bibliotek browse location out of a query string.
   *
   * The SPA navigates by React state and (before this) never touched the URL,
   * so a refresh inside a subject bounced back to the top-level grid. We now
   * push the location to the URL (?subject=&area=[&imageType=]) and read it back
   * on load to RESTORE where the owner was — reusing the exact same
   * URLSearchParams parsing the Skapa deep-link added (skapaLink / skapa_data's
   * readSelectionFromQuery).
   *
   * Pure: parses the given search string only (no window/DOM access — the view
   * passes ``window.location.search``). Accepts the string with or without a
   * leading "?". The values are taxonomy slugs, validated by the caller against
   * the loaded tree before use (a stale/hand-edited URL can't select a bogus
   * node — it just falls back to the top level). Returns
   * ``{ subject|null, area|null, imageType|null }``.
   */
  function readLocationFromQuery(search) {
    const out = { subject: null, area: null, imageType: null };
    if (!search || typeof search !== "string") return out;
    const params = new URLSearchParams(search.charAt(0) === "?" ? search.slice(1) : search);
    out.subject = params.get("subject") || null;
    // area is subject-scoped — only meaningful alongside a subject.
    out.area = out.subject ? (params.get("area") || null) : null;
    out.imageType = params.get("imageType") || params.get("image_type") || null;
    return out;
  }

  /**
   * Build the Bibliotek URL (path + query) for a browse location, so a refresh
   * stays put. Mirrors skapaLink's encode-each-id discipline. The area/imageType
   * are only ever included alongside a subject (areas are subject-scoped). Used
   * with history.replaceState so navigating doesn't spam the back-stack but a
   * reload restores the spot. Returns:
   *   • "/bibliotek"                                  (top-level, no subject),
   *   • "/bibliotek?subject=<s>"                       (subject only),
   *   • "/bibliotek?subject=<s>&area=<a>"              (+ area),
   *   • "/bibliotek?subject=<s>&area=<a>&imageType=<t>" (+ type).
   */
  function bibliotekUrl(subjectId, areaId, imageType) {
    if (!subjectId) return "/bibliotek";
    let qs = `subject=${encodeURIComponent(subjectId)}`;
    if (areaId) {
      qs += `&area=${encodeURIComponent(areaId)}`;
      if (imageType) qs += `&imageType=${encodeURIComponent(imageType)}`;
    }
    return `/bibliotek?${qs}`;
  }

  /* ----------------------------------------------------------------------
   * Review verdict helpers (lb-review-regen)
   * --------------------------------------------------------------------- */

  // review_ok tri-state: null = unreviewed (pending), 1 = OK, 0 = Inte-OK.
  const REVIEW = { PENDING: null, OK: 1, NOTOK: 0 };

  /** Swedish label for a verdict, for the tile/lightbox badge. */
  function reviewLabel(ok) {
    if (ok === 1) return "OK";
    if (ok === 0) return "Inte OK";
    return "Ej granskad";
  }

  /** CSS state class for a verdict badge (styled in components.css). */
  function reviewClass(ok) {
    if (ok === 1) return "ok";
    if (ok === 0) return "notok";
    return "pending";
  }

  /**
   * The next verdict when the OK/Inte-OK toggle is clicked. Clicking the verdict
   * that is already set clears it back to pending (null) — the atelier toggle
   * behaviour; clicking the other verdict switches to it. Pure (kept here so the
   * review control's one bit of state logic is unit-testable in lockstep with
   * the JSX). ``current`` is the present review_ok; ``clicked`` is 1 or 0.
   */
  function toggleVerdict(current, clicked) {
    return current === clicked ? null : clicked;
  }

  /** Does an item match a verdict filter ("all"|"pending"|"ok"|"notok")? Pure. */
  function matchesReview(item, filter) {
    if (!filter || filter === "all") return true;
    const ok = item ? item.review_ok : undefined;
    if (filter === "pending") return ok === null || ok === undefined;
    if (filter === "ok") return ok === 1;
    if (filter === "notok") return ok === 0;
    return true;
  }

  /* ----------------------------------------------------------------------
   * Version-chain helpers (lb-review-regen)
   * --------------------------------------------------------------------- */

  /**
   * Walk a list of library items into version chains via ``parent_id``.
   *
   * A regenerate produces a new row whose ``parent_id`` is the row it was made
   * from, so the items form forests of version chains. This groups a flat item
   * list into one entry per chain ROOT, each carrying its ordered ``versions``
   * (root first → newest last), so the gallery can show one tile per asset with
   * a "v1 · v2 · v3" history and put the Regenerera button on the LATEST version.
   *
   * Robust to a partial page: an item whose parent isn't in the list is treated
   * as its own root (the gallery is area-scoped + may be filtered, so a parent
   * can legitimately be absent). Order within a chain is by ``created_at`` then
   * id (stable); roots keep the order their newest version appears in the input
   * (so a freshly-regenerated chain still surfaces near the top). Pure; never
   * mutates the input. Returns ``[{ root, versions: [...], latest }]``.
   */
  function buildVersionChains(items) {
    const list = Array.isArray(items) ? items : [];
    const byId = new Map(list.map((it) => [it.id, it]));
    // Find the root of an item by following parent_id while the parent is in the
    // page; guard against a cycle (shouldn't happen — ON DELETE SET NULL + a
    // forward-only chain — but never loop forever).
    function rootOf(it) {
      let cur = it;
      const seen = new Set();
      while (cur && cur.parent_id && byId.has(cur.parent_id) && !seen.has(cur.id)) {
        seen.add(cur.id);
        cur = byId.get(cur.parent_id);
      }
      return cur;
    }
    const chains = new Map(); // rootId -> versions[]
    for (const it of list) {
      const root = rootOf(it);
      const key = root.id;
      if (!chains.has(key)) chains.set(key, []);
      chains.get(key).push(it);
    }
    const cmp = (a, b) => {
      const at = a.created_at || "", bt = b.created_at || "";
      if (at < bt) return -1;
      if (at > bt) return 1;
      return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
    };
    const out = [];
    for (const [rootId, versions] of chains) {
      versions.sort(cmp);
      out.push({
        root: byId.get(rootId) || versions[0],
        versions,
        latest: versions[versions.length - 1],
      });
    }
    // Keep roots ordered by their latest version's position in the input
    // (newest-first lists stay newest-first at the chain level).
    const rank = new Map(list.map((it, i) => [it.id, i]));
    out.sort((a, b) => (rank.get(a.latest.id) ?? 0) - (rank.get(b.latest.id) ?? 0));
    return out;
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
    REVIEW,
    orderSubjects,
    findSubject,
    findArea,
    totalAssets,
    centralText,
    subjColor,
    subjSoft,
    skapaLink,
    fmtDate,
    readLocationFromQuery,
    bibliotekUrl,
    reviewLabel,
    reviewClass,
    toggleVerdict,
    matchesReview,
    buildVersionChains,
  };
});
