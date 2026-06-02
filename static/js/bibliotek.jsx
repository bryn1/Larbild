/* ============================================================================
   Lärbild — Bibliotek view (lb-eee + lb-review-regen)
   ----------------------------------------------------------------------------
   The 3-level browse: ämnen (subjects) → områden (areas) → bilder/filmer,
   with a Centralt-innehåll (LGR22 central content) panel for the selected area.
   Adapted faithfully from the design screen (design/screens/bibliotek.jsx +
   ui.jsx) but wired to the live backend instead of the mock window.LB:

     • GET /api/library  — the auto-sorted tree + per-node asset counts (lb-5rc).
     • GET /api/library/items — the per-node gallery items, now carrying each
                           item's review state (review_ok/comment/reviewed_at)
                           and parent_id (the version chain). ?review= filters.
     • GET /api/taxonomy — best-effort bootstrap (image/video types etc.).

   lb-review-regen adds, mirroring the atelier /admin/review UX the owner knows:
     • a review FILTER (Allt / Att granska / Inte OK) over the gallery,
     • per-asset OK / Inte OK verdict + a comment, persisted via
       POST /api/library/<id>/review,
     • a "Regenerera" action (POST /api/library/<id>/regenerate) that enqueues a
       NEW, linked version (parent_id) incorporating the owner's note — repeatable
       across turns, with the version chain shown in the lightbox, and
     • URL persistence: the browse location is pushed to the URL
       (?subject=&area=[&imageType=]) and restored on load, so a refresh inside a
       subject stays put instead of bouncing to the top (the reported nav bug).

   Pure data/ordering logic lives in bibliotek_data.js (window.LBData), unit-
   tested under node. This file is the React rendering layer. No build step —
   Babel-in-browser, matching the design's serving model.
   SFW-only: educational browse + review of the SFW library; no restricted feature.
   ============================================================================ */
const { useState, useEffect, useCallback, useRef } = React;
const D = window.LBData;

/* ---------- minimal icon set (subset of the design's ui.jsx I) ---------- */
const Ico = ({ d, size = 18, sw = 1.6, fill = "none" }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill={fill} stroke="currentColor"
    strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round">{d}</svg>
);
const I = {
  plus:    <Ico d={<path d="M12 5v14M5 12h14" />} sw={2} />,
  lessons: <Ico d={<><path d="M4 5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z" /><path d="M13 3v5h5" /></>} />,
  sparkle: <Ico d={<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z" />} />,
  play:    <Ico d={<path d="M7 5l12 7-12 7z" />} fill="currentColor" sw={0} />,
  close:   <Ico d={<path d="M6 6l12 12M18 6L6 18" />} sw={2} size={20} />,
  check:   <Ico d={<path d="M5 12l5 5L20 7" />} sw={2} size={15} />,
  cross:   <Ico d={<path d="M6 6l12 12M18 6L6 18" />} sw={2} size={15} />,
  redo:    <Ico d={<><path d="M21 12a9 9 0 1 1-3-6.7" /><path d="M21 4v5h-5" /></>} size={16} />,
};

/* ---------- LEVEL 1 card: one subject ---------- */
function SubjectCard({ s, onOpen }) {
  return (
    <div className="subj-card" style={{ "--h": s.hue != null ? s.hue : 268 }} onClick={() => onOpen(s.id)}>
      <div className="sc-top">
        <div className="sc-glyph">{s.glyph}</div>
        <div className="sc-count">{s.asset_count || 0} objekt</div>
      </div>
      <h3>{s.name}</h3>
      <div className="sc-blurb">{s.blurb}</div>
      <div className="sc-areas">
        {(s.areas || []).slice(0, 4).map((a) => <span key={a.id} className="sc-area">{a.name}</span>)}
        {(s.areas || []).length > 4 && <span className="sc-more">+{s.areas.length - 4}</span>}
      </div>
    </div>
  );
}

/* ---------- a verdict badge (tile + lightbox) ---------- */
function ReviewBadge({ ok, onTile }) {
  // Pending verdicts get no tile badge (keeps the grid calm — only a decision
  // shows); the lightbox always shows the current state for clarity.
  if (onTile && (ok === null || ok === undefined)) return null;
  return (
    <span className={`rev-badge ${D.reviewClass(ok)}`}>
      <span className="dot" />{D.reviewLabel(ok)}
    </span>
  );
}

/* ---------- an asset tile (one version chain → its latest version) ---------- */
// The tile shows the LATEST version of a chain; a version-count pill marks a
// chain with history. The image src is the servable URL the API attaches
// (item.thumb_url / item.url, each "/media/<file_path>"); falls back to building
// it from the stored relative path. Clicking opens the full-size lightbox.
function assetThumbSrc(item) {
  return item.thumb_url || item.url ||
    ((item.thumb_path || item.file_path) ? `/media/${item.thumb_path || item.file_path}` : null);
}
// Which library kinds are VIDEO (rendered with a <video> + a play badge): the P6
// film plus the lb-lesson pedagogical kinds (a per-delområde 'lesson' overview +
// a per-begrepp/family 'short'). Kept pure + in lockstep with the node test
// (tests/bibliotek_kind.test.mjs) so the kind→media decision is pinned without a
// DOM/Babel runtime.
function isVideoKind(kind) {
  return kind === "film" || kind === "lesson" || kind === "short";
}
// The Swedish display label for a lesson/short's level tag (the three school
// levels; null/absent → no label). Shown on a video card so the three level-
// versions of one delområde overview are distinguishable at a glance.
function levelLabel(level) {
  return ({ enkel: "Enkel", utvecklad: "Utvecklad", valutvecklad: "Välutvecklad" })[level] || null;
}
function AssetCard({ chain, subject, onOpen }) {
  const item = chain.latest;
  const src = assetThumbSrc(item);
  const versionCount = chain.versions.length;
  return (
    <div className="acard" onClick={() => onOpen && onOpen(chain)}>
      <div className="thumb">
        {src ? <img src={src} alt={item.title} loading="lazy" /> : null}
        <span className={`kind-tag ${isVideoKind(item.kind) ? "film" : ""}`}>{item.kind}</span>
        {isVideoKind(item.kind) && <span className="play">{I.play}</span>}
        {levelLabel(item.level) && <span className="level-pill">{levelLabel(item.level)}</span>}
        <ReviewBadge ok={item.review_ok} onTile />
        {versionCount > 1 && <span className="ver-pill">v{versionCount}</span>}
      </div>
      <div className="ainfo">
        <div className="atitle">{item.title}</div>
        <div className="ameta">
          <span className="subj-dot" style={{ background: D.subjColor(subject), width: 8, height: 8, borderRadius: 2 }} />
          {subject ? subject.short : ""} · {item.area_name || item.area}
        </div>
        {item.lora && <div className="alora">{I.sparkle}{item.lora}</div>}
      </div>
    </div>
  );
}

/* ---------- the review control block (lightbox) ---------- */
// OK / Inte OK toggle + a comment textarea, persisted via POST
// /api/library/<id>/review. Mirrors the atelier review UX. Optimistic on the
// verdict (instant toggle), debounced-on-blur for the comment; the persisted
// state flows back up via onPersist so the gallery tile + filter stay correct.
function ReviewControl({ item, onPersist }) {
  const [ok, setOk] = useState(item.review_ok ?? null);
  const [comment, setComment] = useState(item.review_comment || "");
  const [status, setStatus] = useState("idle"); // idle | saving | saved | err

  // Re-sync when a different asset opens in the modal.
  useEffect(() => {
    setOk(item.review_ok ?? null);
    setComment(item.review_comment || "");
    setStatus("idle");
  }, [item.id]);

  const save = useCallback((body) => {
    setStatus("saving");
    return fetch(`/api/library/${encodeURIComponent(item.id)}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then((r) => { if (!r.ok) throw new Error(`review ${r.status}`); return r.json(); })
      .then((saved) => {
        setStatus("saved");
        // Reflect the persisted state upward (tile badge + filter recompute).
        if (onPersist) onPersist(item.id, {
          review_ok: saved.review_ok,
          review_comment: saved.review_comment,
          reviewed_at: saved.reviewed_at,
        });
        return saved;
      })
      .catch(() => setStatus("err"));
  }, [item.id, onPersist]);

  // Toggle a verdict: clicking the active one clears it back to pending (null).
  // The decision lives in D.toggleVerdict (pure, unit-tested in lockstep).
  const setVerdict = (val) => {
    const next = D.toggleVerdict(ok, val);
    setOk(next);
    save({ review_ok: next });
  };
  const saveComment = () => {
    if ((item.review_comment || "") === comment) return; // no change
    save({ comment });
  };

  return (
    <div className="lb-review">
      <div className="rh">Granskning</div>
      <div className="rev-buttons">
        <button className={`rev-btn ok ${ok === 1 ? "active" : ""}`}
          onClick={() => setVerdict(1)} aria-pressed={ok === 1}>
          {I.check} OK
        </button>
        <button className={`rev-btn notok ${ok === 0 ? "active" : ""}`}
          onClick={() => setVerdict(0)} aria-pressed={ok === 0}>
          {I.cross} Inte OK
        </button>
      </div>
      <textarea className="rev-comment" value={comment}
        placeholder="Kommentar — vad ska ändras? (styr även en regenerering)"
        onChange={(e) => setComment(e.target.value)} onBlur={saveComment} />
      <div className="rev-actions">
        <button className="btn sm" onClick={saveComment} disabled={status === "saving"}>Spara kommentar</button>
        <span className={`rev-status ${status === "saved" ? "saved" : status === "err" ? "err" : ""}`}>
          {status === "saving" ? "Sparar…" : status === "saved" ? "Sparat" : status === "err" ? "Kunde inte spara" : ""}
        </span>
      </div>
    </div>
  );
}

/* ---------- the version chain strip (lightbox) ---------- */
function VersionStrip({ chain, currentId, onPick }) {
  if (!chain || chain.versions.length < 2) return null;
  return (
    <div className="lb-versions">
      <div className="rh">Versioner · {chain.versions.length}</div>
      <div className="ver-strip">
        {chain.versions.map((v, i) => {
          const src = assetThumbSrc(v);
          return (
            <div key={v.id} className={`ver-thumb ${v.id === currentId ? "current" : ""}`}
              title={`v${i + 1}`} onClick={() => onPick(v)}>
              {src ? <img src={src} alt={`v${i + 1}`} /> : null}
              <span className="vn">v{i + 1}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* ---------- full-size lightbox: one chain, the selected version + review ----------
   Opened by clicking a tile (which passes its whole version chain). Shows the
   selected version full-resolution + its provenance + the review control + the
   version strip + a "Regenerera" button (only on the LATEST version — a regen
   always extends the newest tip). Closes on the backdrop, the × button, or
   Escape. SFW educational artefact. */
function Lightbox({ chain, subject, onClose, onPersist, onRegenerated }) {
  // Which version of the chain is on stage (defaults to the latest).
  const [viewId, setViewId] = useState(chain.latest.id);
  const [regen, setRegen] = useState({ phase: "idle", msg: "" }); // idle|busy|done|err

  useEffect(() => { setViewId(chain.latest.id); setRegen({ phase: "idle", msg: "" }); }, [chain.latest.id]);
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const item = chain.versions.find((v) => v.id === viewId) || chain.latest;
  const isLatest = item.id === chain.latest.id;
  const full = item.url || assetThumbSrc(item);
  const when = D.fmtDate ? D.fmtDate(item.generated_at) : (item.generated_at || "");
  const meta = [
    item.lora && ["LoRA", item.lora + (item.lora_strength != null ? ` · ${item.lora_strength}` : "")],
    item.seed != null && ["Seed", String(item.seed)],
    item.backend && ["Backend", item.backend],
    (item.width && item.height) && ["Mått", `${item.width}×${item.height}`],
    when && ["Skapad", when],
  ].filter(Boolean);

  const regenerate = () => {
    setRegen({ phase: "busy", msg: "Startar ny version…" });
    // Regenerate from the version on stage; its current comment (if any) guides
    // the new render server-side. A fresh comment can be left first in the review
    // box (persisted), then it guides the regenerate.
    fetch(`/api/library/${encodeURIComponent(item.id)}/regenerate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    })
      .then((r) => r.json().then((b) => ({ ok: r.ok, status: r.status, b })))
      .then(({ ok, status, b }) => {
        if (!ok) {
          setRegen({ phase: "err", msg: status === 429 ? "Renderkön är full — försök strax igen." : (b.error || "Kunde inte regenerera.") });
          return;
        }
        setRegen({ phase: "done", msg: "Ny version i kö — den dyker upp här när den är klar." });
        if (onRegenerated) onRegenerated(b.job_id); // let the view refresh the node
      })
      .catch(() => setRegen({ phase: "err", msg: "Kunde inte regenerera." }));
  };

  return (
    <div className="lb-backdrop" onClick={onClose}>
      <div className="lb-modal" onClick={(e) => e.stopPropagation()}>
        <button className="lb-close" onClick={onClose} aria-label="Stäng">{I.close}</button>
        <div className="lb-stage">
          {isVideoKind(item.kind)
            ? <video src={full} controls style={{ maxWidth: "100%", maxHeight: "72vh" }} />
            : (full ? <img src={full} alt={item.title} /> : <div className="dim">Ingen bild</div>)}
        </div>
        <div className="lb-info">
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
            <div className="lb-title serif">{item.title}</div>
            <ReviewBadge ok={item.review_ok} />
          </div>
          <div className="lb-sub mono">
            {subject ? subject.name : ""} · {item.area_name || item.area}
            {item.image_type ? " · " + item.image_type : ""}
            {chain.versions.length > 1 ? ` · v${chain.versions.indexOf(item) + 1}/${chain.versions.length}` : ""}
          </div>
          {item.prompt && <div className="lb-prompt">{item.prompt}</div>}
          {meta.length > 0 && (
            <dl className="lb-meta">
              {meta.map(([k, v]) => (
                <div className="lb-meta-row" key={k}><dt>{k}</dt><dd>{v}</dd></div>
              ))}
            </dl>
          )}

          {/* Review the version on stage (verdict + note). Films are reviewable
              too; only regeneration is image-only (the button is gated below). */}
          <ReviewControl item={item} onPersist={onPersist} />

          {/* Regenerate — only the latest version of an image chain (a regen
              extends the newest tip; films aren't regenerable in v1). */}
          {item.kind === "bild" && (
            <div className="rev-actions" style={{ marginTop: 14 }}>
              <button className="btn primary" onClick={regenerate}
                disabled={regen.phase === "busy" || !isLatest}
                title={isLatest ? "Skapa en ny version utifrån kommentaren" : "Öppna senaste versionen för att regenerera"}>
                {I.redo} Regenerera
              </button>
              {!isLatest && <span className="rev-status">Visar en äldre version</span>}
              {regen.msg && (
                <span className={`rev-status ${regen.phase === "err" ? "err" : regen.phase === "done" ? "saved" : ""}`}>
                  {regen.msg}
                </span>
              )}
            </div>
          )}

          <VersionStrip chain={chain} currentId={item.id} onPick={(v) => setViewId(v.id)} />
        </div>
      </div>
    </div>
  );
}

/* ---------- the Bibliotek view ---------- */
function Bibliotek() {
  const [subjects, setSubjects] = useState(null); // /api/library tree (raw order)
  const [error, setError] = useState(null);
  const [subjectId, setSubjectId] = useState(null);
  const [areaId, setAreaId] = useState(null);
  const [kind, setKind] = useState("all"); // all | bild | film | lektion (lesson+short)
  const [reviewFilter, setReviewFilter] = useState("all"); // all | pending | notok
  // Per-node item cache keyed by "<subject>/<area>" — the gallery's data, fetched
  // lazily from /api/library/items when a node is opened. `null` = in-flight;
  // array = loaded. A bump counter forces a re-fetch of the open node after a
  // regenerate so the new version appears.
  const [itemsByNode, setItemsByNode] = useState({});
  const [lightbox, setLightbox] = useState(null); // the version chain open in the modal, or null
  // The deep-linked location read from the URL on first load (lb-review-regen):
  // restored once the tree arrives so a refresh stays put. Captured in a ref so
  // it's read exactly once (not re-applied on every render).
  const initialLoc = useRef(D.readLocationFromQuery(
    typeof window !== "undefined" ? window.location.search : ""));
  const restored = useRef(false);

  // Bootstrap from the live backend. /api/library is the primary source.
  useEffect(() => {
    let alive = true;
    Promise.all([
      fetch("/api/library").then((r) => { if (!r.ok) throw new Error(`/api/library ${r.status}`); return r.json(); }),
      fetch("/api/taxonomy").then((r) => (r.ok ? r.json() : null)).catch(() => null),
    ])
      .then(([lib]) => { if (alive) setSubjects(lib.subjects || []); })
      .catch((e) => { if (alive) setError(String(e)); });
    return () => { alive = false; };
  }, []);

  // RESTORE the browse location from the URL once the tree is loaded — validated
  // against the live taxonomy so a stale/bogus URL can't select an invalid node
  // (it just lands at the top level). Runs once.
  useEffect(() => {
    if (!subjects || restored.current) return;
    restored.current = true;
    const loc = initialLoc.current;
    const sub = loc.subject ? D.findSubject(subjects, loc.subject) : null;
    if (!sub) return; // unknown/absent subject → stay at top level
    setSubjectId(sub.id);
    const ar = loc.area ? D.findArea(sub, loc.area) : null;
    if (ar) setAreaId(ar.id);
  }, [subjects]);

  // PUSH the current location to the URL so a refresh stays put. replaceState
  // (not pushState) so internal nav doesn't bloat the back-stack; a reload reads
  // it back via initialLoc above. Skipped until the first restore has run, so we
  // never stomp the deep-link before adopting it.
  useEffect(() => {
    if (!restored.current || typeof window === "undefined") return;
    const url = D.bibliotekUrl(subjectId, areaId, null);
    if (window.location.pathname + window.location.search !== url) {
      window.history.replaceState(null, "", url);
    }
  }, [subjectId, areaId]);

  // Apply the active subject's hue to the page accent.
  const sub = subjects ? D.findSubject(subjects, subjectId) : null;
  useEffect(() => {
    const h = sub && sub.hue != null ? sub.hue : 268;
    document.documentElement.style.setProperty("--brand-h", h);
  }, [sub]);

  // Merge a persisted review back into the item cache so the tile badge + the
  // review filter recompute without a refetch (passed to the lightbox).
  const applyReview = useCallback((id, patch) => {
    setItemsByNode((prev) => {
      const next = {};
      for (const key of Object.keys(prev)) {
        const arr = prev[key];
        next[key] = Array.isArray(arr)
          ? arr.map((it) => (it.id === id ? { ...it, ...patch } : it))
          : arr;
      }
      return next;
    });
  }, []);

  // Force a refetch of the open node (used after a regenerate so the queued new
  // version shows once it renders — the owner can also just reopen the area).
  const refetchOpenNode = useCallback(() => {
    if (!sub) return;
    const ids = areaId ? [areaId] : (sub.areas || []).filter((a) => (a.asset_count || 0) > 0).map((a) => a.id);
    setItemsByNode((prev) => {
      const next = { ...prev };
      ids.forEach((aid) => { delete next[`${sub.id}/${aid}`]; });
      return next;
    });
  }, [sub, areaId]);

  // Lazily fetch the gallery items for the open node from /api/library/items.
  useEffect(() => {
    if (!sub) return;
    const wanted = (areaId
      ? (sub.areas || []).filter((a) => a.id === areaId)
      : (sub.areas || []).filter((a) => (a.asset_count || 0) > 0)
    ).map((a) => a.id);
    const toLoad = wanted.filter((aid) => itemsByNode[`${sub.id}/${aid}`] === undefined);
    if (toLoad.length === 0) return;

    let alive = true;
    setItemsByNode((prev) => {
      const next = { ...prev };
      toLoad.forEach((aid) => { next[`${sub.id}/${aid}`] = null; });
      return next;
    });
    toLoad.forEach((aid) => {
      const qs = `subject=${encodeURIComponent(sub.id)}&area=${encodeURIComponent(aid)}`;
      fetch(`/api/library/items?${qs}`)
        .then((r) => (r.ok ? r.json() : { items: [] }))
        .then((data) => {
          if (!alive) return;
          setItemsByNode((prev) => ({ ...prev, [`${sub.id}/${aid}`]: data.items || [] }));
        })
        .catch(() => {
          if (!alive) return;
          setItemsByNode((prev) => ({ ...prev, [`${sub.id}/${aid}`]: [] }));
        });
    });
    return () => { alive = false; };
  }, [sub, areaId, itemsByNode]);

  if (error) {
    return (
      <div className="page" data-screen-label="Bibliotek">
        <div className="page-body">
          <div className="empty"><div className="pull">Kunde inte läsa biblioteket.</div>
            <div className="dim mono">{error}</div></div>
        </div>
      </div>
    );
  }
  if (!subjects) {
    return (
      <div className="page" data-screen-label="Bibliotek">
        <div className="page-body"><div className="empty"><div className="dim">Läser in…</div></div></div>
      </div>
    );
  }

  const ordered = D.orderSubjects(subjects); // fysik/teknik/kemi/biologi first

  // ---------- LEVEL 1: all subjects ----------
  if (!sub) {
    return (
      <div className="page" data-screen-label="Bibliotek">
        <div className="page-head">
          <div>
            <h1 className="serif">Bild<em>biblioteket</em></h1>
            <div className="caption">{D.totalAssets(subjects)} objekt · {subjects.length} ämnen · sorterat enligt LGR22 åk 7–9</div>
          </div>
          <div className="page-head-actions">
            <a className="btn primary" href={D.skapaLink()}>{I.plus} Skapa bild</a>
          </div>
        </div>
        <div className="page-body">
          <div className="subj-grid">
            {ordered.map((s) => <SubjectCard key={s.id} s={s} onOpen={setSubjectId} />)}
          </div>
        </div>
      </div>
    );
  }

  // ---------- LEVEL 2/3: inside a subject ----------
  const area = areaId ? D.findArea(sub, areaId) : null;
  const nodeKey = (aid) => `${sub.id}/${aid}`;
  const relevantAreaIds = area
    ? [area.id]
    : (sub.areas || []).filter((a) => (a.asset_count || 0) > 0).map((a) => a.id);
  const itemsLoading = relevantAreaIds.some((aid) => !Array.isArray(itemsByNode[nodeKey(aid)]));
  let assets = relevantAreaIds.flatMap((aid) => itemsByNode[nodeKey(aid)] || []);
  // The "lektion" tab groups the lb-lesson pedagogical videos (a per-delområde
  // 'lesson' overview + a per-begrepp/family 'short'); the other tabs match an
  // exact kind. "all" applies no kind filter.
  if (kind === "lektion") assets = assets.filter((it) => it.kind === "lesson" || it.kind === "short");
  else if (kind !== "all") assets = assets.filter((it) => it.kind === kind);
  if (reviewFilter !== "all") assets = assets.filter((it) => D.matchesReview(it, reviewFilter));
  // Group into version chains — one tile per asset, latest version on top, with
  // its history available in the lightbox. The filters above run on items first
  // so a chain shows iff its latest matching version qualifies.
  const chains = D.buildVersionChains(assets);
  // Keep the lightbox's chain in sync with fresh data (e.g. after a review write
  // or refetch) so its review state + version strip reflect the cache.
  const liveLightboxChain = lightbox
    ? (chains.find((c) => c.root.id === lightbox.root.id) || lightbox)
    : null;

  return (
    <div className="page" data-screen-label={"Bibliotek · " + sub.name}>
      <div className="page-head">
        <div>
          <div className="crumb" style={{ marginBottom: 8 }}>
            <a onClick={() => { setSubjectId(null); setAreaId(null); }}>Bibliotek</a>
            <span className="sep">/</span>
            <a onClick={() => setAreaId(null)}>{sub.name}</a>
            {area && <><span className="sep">/</span><span>{area.name}</span></>}
          </div>
          <h1 className="serif" style={{ display: "flex", alignItems: "center", gap: 14 }}>
            <span style={{ width: 44, height: 44, borderRadius: 12, display: "grid", placeItems: "center", fontSize: 24, background: D.subjSoft(sub), color: D.subjColor(sub, 0.45) }}>{sub.glyph}</span>
            {area ? area.name : sub.name}
          </h1>
          <div className="caption">{(itemsLoading && kind === "all" && reviewFilter === "all" ? (area ? (area.asset_count || 0) : (sub.asset_count || 0)) : chains.length)} objekt{area ? "" : " · " + (sub.areas || []).length + " områden"}</div>
        </div>
        <div className="page-head-actions">
          <div className="tabs">
            <button className={`tab ${kind === "all" ? "active" : ""}`} onClick={() => setKind("all")}>Allt</button>
            <button className={`tab ${kind === "bild" ? "active" : ""}`} onClick={() => setKind("bild")}>Bilder</button>
            <button className={`tab ${kind === "film" ? "active" : ""}`} onClick={() => setKind("film")}>Filmer</button>
            <button className={`tab ${kind === "lektion" ? "active" : ""}`} onClick={() => setKind("lektion")}>Lektioner</button>
          </div>
          <span className="hsep" />
          {/* Review filter — the atelier-style triage lens over the gallery. */}
          <div className="tabs">
            <button className={`tab ${reviewFilter === "all" ? "active" : ""}`} onClick={() => setReviewFilter("all")} title="Visa alla">Alla</button>
            <button className={`tab ${reviewFilter === "pending" ? "active" : ""}`} onClick={() => setReviewFilter("pending")} title="Ej granskade">Att granska</button>
            <button className={`tab ${reviewFilter === "notok" ? "active" : ""}`} onClick={() => setReviewFilter("notok")} title="Markerade Inte OK">Inte OK</button>
          </div>
          <span className="hsep" />
          <a className="btn primary" href={D.skapaLink(sub.id, area ? area.id : null)}>{I.plus} Skapa här</a>
        </div>
      </div>

      <div className="page-body" style={{ display: "grid", gridTemplateColumns: "236px 1fr", gap: 28, padding: 0 }}>
        {/* område-rail (LEVEL 2) */}
        <div style={{ borderRight: "1px solid var(--border)", padding: "24px 18px 40px", overflowY: "auto" }}>
          <div className="section-h"><span>Områden</span></div>
          <div className="area-rail">
            <button className={`area-item ${!area ? "active" : ""}`} onClick={() => setAreaId(null)}>
              <span className="ag">◆</span>
              <span className="an">Alla områden</span>
              <span className="ac">{sub.asset_count || 0}</span>
            </button>
            {(sub.areas || []).map((a) => (
              <button key={a.id} className={`area-item ${areaId === a.id ? "active" : ""}`} onClick={() => setAreaId(a.id)} title={D.centralText(a)}>
                <span className="ag">{a.glyph}</span>
                <span className="an">{a.name}</span>
                <span className="ac">{a.asset_count || 0}</span>
              </button>
            ))}
          </div>
        </div>

        {/* assets + Centralt-innehåll panel (LEVEL 3) */}
        <div style={{ padding: "24px 34px 48px", overflowY: "auto" }}>
          {area && (
            <div className="panel" style={{ marginBottom: 22, background: D.subjSoft(sub), borderColor: "var(--brand-line)" }}>
              <div className="panel-body" style={{ padding: "13px 16px", fontSize: 13, color: "var(--ink-2)" }}>
                <span className="upper" style={{ color: "var(--brand-ink)", marginRight: 10 }}>Centralt innehåll</span>
                {D.centralText(area)}
              </div>
            </div>
          )}
          {chains.length ? (
            <div className="asset-grid">
              {chains.map((c) => <AssetCard key={c.root.id} chain={c} subject={sub} onOpen={setLightbox} />)}
            </div>
          ) : itemsLoading ? (
            <div className="empty"><div className="dim">Läser in bilder…</div></div>
          ) : (
            <div className="empty">
              <div className="pull">
                {reviewFilter === "pending" ? "Inget kvar att granska här." :
                  reviewFilter === "notok" ? "Inga objekt markerade Inte OK här." :
                  `Inga ${kind === "film" ? "filmer" : kind === "bild" ? "bilder" : kind === "lektion" ? "lektioner" : "objekt"} här ännu.`}
              </div>
              <div className="dim" style={{ marginBottom: 16 }}>
                {reviewFilter !== "all" ? "Byt filter för att se fler objekt." : "Allt du skapar för det här området hamnar automatiskt här."}
              </div>
              {reviewFilter === "all" && (
                <a className="btn primary" style={{ margin: "0 auto" }} href={D.skapaLink(sub.id, area ? area.id : null)}>{I.plus} Skapa det första</a>
              )}
            </div>
          )}
        </div>
      </div>
      {liveLightboxChain && (
        <Lightbox
          chain={liveLightboxChain}
          subject={sub}
          onClose={() => setLightbox(null)}
          onPersist={applyReview}
          onRegenerated={refetchOpenNode}
        />
      )}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<Bibliotek />);
