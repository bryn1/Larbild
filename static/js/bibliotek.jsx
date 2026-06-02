/* ============================================================================
   Lärbild — Bibliotek view (lb-eee)
   ----------------------------------------------------------------------------
   The 3-level browse: ämnen (subjects) → områden (areas) → bilder/filmer,
   with a Centralt-innehåll (LGR22 central content) panel for the selected area.
   Adapted faithfully from the design screen (design/screens/bibliotek.jsx +
   ui.jsx) but wired to the live backend instead of the mock window.LB:

     • GET /api/library  — the auto-sorted tree + per-node asset counts (lb-5rc).
                           This is the primary source: structure, counts, and
                           the per-subject display fields (hue/glyph/blurb) all
                           arrive in one call.
     • GET /api/taxonomy — also fetched, as the broader bootstrap the design
                           bootstraps from (image/video types etc.); the view
                           tolerates it being unavailable.

   Pure data/ordering logic lives in bibliotek_data.js (window.LBData), unit-
   tested under node. This file is the React rendering layer. No build step —
   Babel-in-browser, matching the design's serving model.

   Reuses the design-system component classes from static/css (subj-card,
   area-rail, asset-grid, panel, crumb, tabs, empty) — no inline color/spacing
   beyond the documented per-subject hue mechanism.
   SFW-only: educational browse of the SFW library; no restricted feature.
   ============================================================================ */
const { useState, useEffect } = React;
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

/* ---------- an asset tile (bild/film) ---------- */
// The image src is the servable URL the API attaches (item.thumb_url / item.url,
// each "/media/<file_path>"); falls back to building it from the stored relative
// path for forward-compat with any caller that hasn't been updated. Clicking the
// tile opens the full-size lightbox.
function assetThumbSrc(item) {
  return item.thumb_url || item.url ||
    ((item.thumb_path || item.file_path) ? `/media/${item.thumb_path || item.file_path}` : null);
}
function AssetCard({ item, subject, onOpen }) {
  const src = assetThumbSrc(item);
  return (
    <div className="acard" onClick={() => onOpen && onOpen(item)}>
      <div className="thumb">
        {src ? <img src={src} alt={item.title} loading="lazy" /> : null}
        <span className={`kind-tag ${item.kind === "film" ? "film" : ""}`}>{item.kind}</span>
        {item.kind === "film" && <span className="play">{I.play}</span>}
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

/* ---------- full-size lightbox: one asset + its metadata ---------- */
// Opened by clicking a tile. Shows the full-resolution image (item.url) and the
// prompt + render provenance (LoRA, seed, backend, dimensions, generated_at).
// Closes on the backdrop, the × button, or Escape. SFW educational artefact.
function Lightbox({ item, subject, onClose }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  if (!item) return null;
  const full = item.url || assetThumbSrc(item);
  const when = D.fmtDate ? D.fmtDate(item.generated_at) : (item.generated_at || "");
  const meta = [
    item.lora && ["LoRA", item.lora + (item.lora_strength != null ? ` · ${item.lora_strength}` : "")],
    item.seed != null && ["Seed", String(item.seed)],
    item.backend && ["Backend", item.backend],
    (item.width && item.height) && ["Mått", `${item.width}×${item.height}`],
    when && ["Skapad", when],
  ].filter(Boolean);
  return (
    <div className="lb-backdrop" onClick={onClose}>
      <div className="lb-modal" onClick={(e) => e.stopPropagation()}>
        <button className="lb-close" onClick={onClose} aria-label="Stäng">{I.close}</button>
        <div className="lb-stage">
          {item.kind === "film"
            ? <video src={full} controls style={{ maxWidth: "100%", maxHeight: "72vh" }} />
            : (full ? <img src={full} alt={item.title} /> : <div className="dim">Ingen bild</div>)}
        </div>
        <div className="lb-info">
          <div className="lb-title serif">{item.title}</div>
          <div className="lb-sub mono">
            {subject ? subject.name : ""} · {item.area_name || item.area}
            {item.image_type ? " · " + item.image_type : ""}
          </div>
          {item.prompt && <div className="lb-prompt">{item.prompt}</div>}
          {meta.length > 0 && (
            <dl className="lb-meta">
              {meta.map(([k, v]) => (
                <div className="lb-meta-row" key={k}><dt>{k}</dt><dd>{v}</dd></div>
              ))}
            </dl>
          )}
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
  const [kind, setKind] = useState("all"); // all | bild | film
  // Per-node item cache keyed by "<subject>/<area>" — the gallery's data, fetched
  // lazily from /api/library/items when a node is opened (the count tree from
  // /api/library has no items). `null` value = in-flight; array = loaded.
  const [itemsByNode, setItemsByNode] = useState({});
  const [lightbox, setLightbox] = useState(null); // the asset open in the modal, or null

  // Bootstrap from the live backend. /api/library is the primary source; the
  // taxonomy call is best-effort (kept for parity with the design bootstrap).
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

  // Apply the active subject's hue to the page accent.
  const sub = subjects ? D.findSubject(subjects, subjectId) : null;
  useEffect(() => {
    const h = sub && sub.hue != null ? sub.hue : 268;
    document.documentElement.style.setProperty("--brand-h", h);
  }, [sub]);

  // Lazily fetch the gallery items for the open node from /api/library/items.
  // When an area is selected → just that area; for "Alla områden" → every area
  // that has assets (asset_count > 0), so the merged "all" view shows real
  // images. Each node is fetched once and cached in itemsByNode; an empty node
  // is never fetched (its asset_count is 0). Best-effort per area: one failed
  // fetch leaves that node's cache empty without breaking the others.
  useEffect(() => {
    if (!sub) return;
    const wanted = (areaId
      ? (sub.areas || []).filter((a) => a.id === areaId)
      : (sub.areas || []).filter((a) => (a.asset_count || 0) > 0)
    ).map((a) => a.id);
    const toLoad = wanted.filter((aid) => itemsByNode[`${sub.id}/${aid}`] === undefined);
    if (toLoad.length === 0) return;

    let alive = true;
    // Mark in-flight so we don't refetch while a request is pending.
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
  // Pull the gallery items from the lazily-fetched cache (itemsByNode), keyed by
  // "<subject>/<area>". For one area → that node; for "Alla områden" → the merged
  // items across every area with assets. Filtered by the kind tab.
  const nodeKey = (aid) => `${sub.id}/${aid}`;
  const relevantAreaIds = area
    ? [area.id]
    : (sub.areas || []).filter((a) => (a.asset_count || 0) > 0).map((a) => a.id);
  // Loading until every relevant node has resolved to an array (null = in-flight,
  // undefined = not yet requested → the effect above will request it).
  const itemsLoading = relevantAreaIds.some((aid) => !Array.isArray(itemsByNode[nodeKey(aid)]));
  let assets = relevantAreaIds.flatMap((aid) => itemsByNode[nodeKey(aid)] || []);
  if (kind !== "all") assets = assets.filter((it) => it.kind === kind);

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
          <div className="caption">{(itemsLoading && kind === "all" ? (area ? (area.asset_count || 0) : (sub.asset_count || 0)) : assets.length)} objekt{area ? "" : " · " + (sub.areas || []).length + " områden"}</div>
        </div>
        <div className="page-head-actions">
          <div className="tabs">
            <button className={`tab ${kind === "all" ? "active" : ""}`} onClick={() => setKind("all")}>Allt</button>
            <button className={`tab ${kind === "bild" ? "active" : ""}`} onClick={() => setKind("bild")}>Bilder</button>
            <button className={`tab ${kind === "film" ? "active" : ""}`} onClick={() => setKind("film")}>Filmer</button>
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
          {assets.length ? (
            <div className="asset-grid">
              {assets.map((it) => <AssetCard key={it.id} item={it} subject={sub} onOpen={setLightbox} />)}
            </div>
          ) : itemsLoading ? (
            <div className="empty"><div className="dim">Läser in bilder…</div></div>
          ) : (
            <div className="empty">
              <div className="pull">Inga {kind === "film" ? "filmer" : kind === "bild" ? "bilder" : "objekt"} här ännu.</div>
              <div className="dim" style={{ marginBottom: 16 }}>Allt du skapar för det här området hamnar automatiskt här.</div>
              <a className="btn primary" style={{ margin: "0 auto" }} href={D.skapaLink(sub.id, area ? area.id : null)}>{I.plus} Skapa det första</a>
            </div>
          )}
        </div>
      </div>
      {lightbox && <Lightbox item={lightbox} subject={sub} onClose={() => setLightbox(null)} />}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<Bibliotek />);
