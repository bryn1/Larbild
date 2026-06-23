/* ============================================================================
   Lärbild — Lektioner (asset-lessons) view  (lb-zdp)
   ----------------------------------------------------------------------------
   Group EXISTING library assets into a named, ordered "lesson". This is a
   SEPARATE concern from the video-manuscript funnel (skapa.jsx / the lb-lesson
   pipeline that RENDERS a video) — it just collects assets the teacher already
   has. Backed by the /api/asset-lessons* endpoints (backend lb-zdp).

   This file adds (owner-only — gated on PUBLIC_READONLY by the caller):
     • LessonsModal — list lessons, create one, open one to see its ordered
       assets (reusing the gallery tile look), remove an asset, delete a lesson.
       Move-up/move-down reorder wired to PUT …/order; drag-reorder is deferred.
     • AddToLessonControl — a small block for the Bibliotek lightbox: add the
       currently-open asset to an existing lesson or a new one (POST …/items).

   Behaviour-additive: it neither alters the gallery/review/regenerate loop nor
   the video funnel. Exposed on window for the Babel-in-browser bibliotek.jsx
   (separate <script> scope). SFW-only: a grouping layer over the SFW library.
   ============================================================================ */
(function () {
  const { useState, useEffect, useCallback } = React;

  const api = {
    list: () => fetch("/api/asset-lessons").then((r) => r.json()),
    create: (body) => fetch("/api/asset-lessons", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }).then((r) => r.json().then((b) => ({ ok: r.ok, b }))),
    get: (id) => fetch(`/api/asset-lessons/${encodeURIComponent(id)}`).then((r) => r.json()),
    del: (id) => fetch(`/api/asset-lessons/${encodeURIComponent(id)}`, { method: "DELETE" }),
    addItem: (id, libraryId) => fetch(`/api/asset-lessons/${encodeURIComponent(id)}/items`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ library_id: libraryId }),
    }).then((r) => r.json().then((b) => ({ ok: r.ok, b }))),
    removeItem: (id, libraryId) => fetch(
      `/api/asset-lessons/${encodeURIComponent(id)}/items/${encodeURIComponent(libraryId)}`,
      { method: "DELETE" }
    ).then((r) => r.json()),
    reorder: (id, order) => fetch(`/api/asset-lessons/${encodeURIComponent(id)}/order`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ order }),
    }).then((r) => r.json()),
  };

  const closeIco = (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M6 6l12 12M18 6L6 18" /></svg>
  );

  function thumbSrc(item) {
    return item.thumb_url || item.url || null;
  }

  /* ---------- a single lesson's detail (its ordered assets) ---------- */
  function LessonDetail({ lessonId, onBack, onChanged }) {
    const [data, setData] = useState(null); // {lesson, items} | null while loading
    const [busy, setBusy] = useState(false);

    const reload = useCallback(() => {
      api.get(lessonId).then(setData).catch(() => setData({ lesson: null, items: [] }));
    }, [lessonId]);
    useEffect(() => { reload(); }, [reload]);

    if (!data) return <div className="empty"><div className="dim">Läser in lektion…</div></div>;
    if (!data.lesson) return <div className="empty"><div className="pull">Lektionen hittades inte.</div></div>;

    const items = data.items || [];
    const move = (idx, dir) => {
      const j = idx + dir;
      if (j < 0 || j >= items.length) return;
      const order = items.map((it) => it.id);
      [order[idx], order[j]] = [order[j], order[idx]];
      setBusy(true);
      api.reorder(lessonId, order).then((b) => { setData((d) => ({ ...d, items: b.items || items })); })
        .finally(() => setBusy(false));
    };
    const remove = (libraryId) => {
      setBusy(true);
      api.removeItem(lessonId, libraryId).then(() => { reload(); if (onChanged) onChanged(); })
        .finally(() => setBusy(false));
    };

    return (
      <div className="lesson-detail">
        <div className="lesson-detail-head">
          <button type="button" className="btn sm ghost" onClick={onBack}>← Alla lektioner</button>
          <div className="ld-title serif">{data.lesson.title}</div>
          <span className="ld-count mono">{items.length} objekt</span>
        </div>
        {items.length === 0 ? (
          <div className="empty">
            <div className="pull">Inga objekt i lektionen ännu.</div>
            <div className="dim">Öppna en bild i biblioteket och välj "Lägg till i lektion".</div>
          </div>
        ) : (
          <div className="lesson-items">
            {items.map((it, idx) => (
              <div className="lesson-item" key={it.id}>
                <span className="li-pos mono">{idx + 1}</span>
                <div className="li-thumb">{thumbSrc(it) ? <img src={thumbSrc(it)} alt={it.title} loading="lazy" /> : null}</div>
                <div className="li-info">
                  <div className="li-name">{it.title}</div>
                  <div className="li-meta mono">{it.subject} · {it.area_name || it.area}</div>
                </div>
                <div className="li-actions">
                  <button type="button" className="icon-btn" title="Flytta upp" disabled={busy || idx === 0} onClick={() => move(idx, -1)}>↑</button>
                  <button type="button" className="icon-btn" title="Flytta ner" disabled={busy || idx === items.length - 1} onClick={() => move(idx, 1)}>↓</button>
                  <button type="button" className="icon-btn danger" title="Ta bort ur lektionen" disabled={busy} onClick={() => remove(it.id)}>✕</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  /* ---------- the lessons list + create ---------- */
  function LessonsList({ onOpen }) {
    const [lessons, setLessons] = useState(null);
    const [title, setTitle] = useState("");
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState(null);

    const reload = useCallback(() => {
      api.list().then((b) => setLessons(b.lessons || [])).catch(() => setLessons([]));
    }, []);
    useEffect(() => { reload(); }, [reload]);

    const create = () => {
      const t = title.trim();
      if (!t) return;
      setBusy(true); setErr(null);
      api.create({ title: t }).then(({ ok, b }) => {
        if (!ok) { setErr(b.error || "Kunde inte skapa lektionen."); return; }
        setTitle(""); reload();
      }).catch(() => setErr("Kunde inte skapa lektionen.")).finally(() => setBusy(false));
    };
    const remove = (id) => {
      setBusy(true);
      api.del(id).then(reload).finally(() => setBusy(false));
    };

    return (
      <div className="lessons-list">
        <div className="lesson-create">
          <input className="lesson-title-input" type="text" value={title} placeholder="Namnge en ny lektion…"
            maxLength={300} onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") create(); }} />
          <button type="button" className="btn primary" disabled={busy || !title.trim()} onClick={create}>Skapa lektion</button>
        </div>
        {err && <div className="rev-status err">{err}</div>}
        {lessons === null ? (
          <div className="empty"><div className="dim">Läser in lektioner…</div></div>
        ) : lessons.length === 0 ? (
          <div className="empty">
            <div className="pull">Inga lektioner ännu.</div>
            <div className="dim">Skapa en ovan, lägg sedan till bilder från biblioteket.</div>
          </div>
        ) : (
          <div className="lesson-cards">
            {lessons.map((l) => (
              <div className="lesson-card" key={l.id}>
                <div className="lc-thumbs" onClick={() => onOpen(l.id)}>
                  {(l.thumb_urls || []).slice(0, 4).map((u, i) => (
                    <div className="lc-thumb" key={i}>{u ? <img src={u} alt="" loading="lazy" /> : null}</div>
                  ))}
                  {(l.thumb_urls || []).length === 0 && <div className="lc-empty dim">Tom</div>}
                </div>
                <div className="lc-foot">
                  <div className="lc-text" onClick={() => onOpen(l.id)}>
                    <div className="lc-title">{l.title}</div>
                    <div className="lc-meta mono">{l.asset_count} objekt</div>
                  </div>
                  <button type="button" className="icon-btn danger" title="Radera lektionen" disabled={busy}
                    onClick={() => remove(l.id)}>✕</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  /* ---------- the Lektioner modal ---------- */
  function LessonsModal({ onClose }) {
    const [openId, setOpenId] = useState(null); // null = list, else lesson detail

    useEffect(() => {
      const onKey = (e) => { if (e.key === "Escape") { if (openId) setOpenId(null); else onClose(); } };
      window.addEventListener("keydown", onKey);
      return () => window.removeEventListener("keydown", onKey);
    }, [onClose, openId]);

    return (
      <div className="lb-backdrop" onClick={onClose}>
        <div className="lessons-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Lektioner">
          <div className="lessons-head">
            <div className="lessons-title serif">Lektioner</div>
            <button type="button" className="lb-close" onClick={onClose} aria-label="Stäng">{closeIco}</button>
          </div>
          <div className="lessons-body">
            {openId
              ? <LessonDetail lessonId={openId} onBack={() => setOpenId(null)} onChanged={() => {}} />
              : <LessonsList onOpen={setOpenId} />}
          </div>
        </div>
      </div>
    );
  }

  /* ---------- "Lägg till i lektion" control (Bibliotek lightbox) ----------
     A compact block: pick an existing lesson from a <select> (or create a new one
     inline) and add the open asset to it. POSTs to /api/asset-lessons/<id>/items
     (idempotent server-side). Shows a transient confirmation. */
  function AddToLessonControl({ item }) {
    const [lessons, setLessons] = useState(null);
    const [sel, setSel] = useState("");
    const [newTitle, setNewTitle] = useState("");
    const [creating, setCreating] = useState(false);
    const [state, setState] = useState({ phase: "idle", msg: "" }); // idle|busy|done|err

    const reload = useCallback(() => {
      api.list().then((b) => setLessons(b.lessons || [])).catch(() => setLessons([]));
    }, []);
    useEffect(() => { reload(); }, [reload]);
    // Reset the transient state when a different asset opens.
    useEffect(() => { setState({ phase: "idle", msg: "" }); }, [item.id]);

    const addTo = (lessonId, label) => {
      setState({ phase: "busy", msg: "Lägger till…" });
      api.addItem(lessonId, item.id).then(({ ok, b }) => {
        if (!ok) { setState({ phase: "err", msg: b.error || "Kunde inte lägga till." }); return; }
        setState({ phase: "done", msg: `Tillagd i "${label}".` });
      }).catch(() => setState({ phase: "err", msg: "Kunde inte lägga till." }));
    };

    const add = () => {
      if (!sel) return;
      const l = (lessons || []).find((x) => x.id === sel);
      addTo(sel, l ? l.title : "lektion");
    };
    const createAndAdd = () => {
      const t = newTitle.trim();
      if (!t) return;
      setState({ phase: "busy", msg: "Skapar lektion…" });
      api.create({ title: t }).then(({ ok, b }) => {
        if (!ok) { setState({ phase: "err", msg: b.error || "Kunde inte skapa lektionen." }); return; }
        setNewTitle(""); setCreating(false); reload();
        addTo(b.lesson.id, b.lesson.title);
      }).catch(() => setState({ phase: "err", msg: "Kunde inte skapa lektionen." }));
    };

    return (
      <div className="add-to-lesson">
        <div className="rh">Lektion</div>
        {!creating ? (
          <div className="atl-row">
            <select className="atl-select" value={sel} onChange={(e) => setSel(e.target.value)}
              aria-label="Välj lektion">
              <option value="">Välj lektion…</option>
              {(lessons || []).map((l) => <option key={l.id} value={l.id}>{l.title} ({l.asset_count})</option>)}
            </select>
            <button type="button" className="btn sm" disabled={!sel || state.phase === "busy"} onClick={add}>Lägg till</button>
            <button type="button" className="btn sm ghost" onClick={() => setCreating(true)}>+ Ny</button>
          </div>
        ) : (
          <div className="atl-row">
            <input className="atl-input" type="text" value={newTitle} maxLength={300}
              placeholder="Namn på ny lektion…" onChange={(e) => setNewTitle(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") createAndAdd(); }} />
            <button type="button" className="btn sm" disabled={!newTitle.trim() || state.phase === "busy"} onClick={createAndAdd}>Skapa &amp; lägg till</button>
            <button type="button" className="btn sm ghost" onClick={() => setCreating(false)}>Avbryt</button>
          </div>
        )}
        {state.msg && (
          <span className={`rev-status ${state.phase === "err" ? "err" : state.phase === "done" ? "saved" : ""}`}>{state.msg}</span>
        )}
      </div>
    );
  }

  Object.assign(window, { LBLessonsModal: LessonsModal, LBAddToLessonControl: AddToLessonControl });
})();
