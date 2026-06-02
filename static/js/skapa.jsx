/* ============================================================================
   Lärbild — Skapa bild (create-image funnel) view (lb-e1p / P3.1)
   ----------------------------------------------------------------------------
   The "Skapa bild" funnel: pick ämne (subject) → område (area) → bildtyp
   (imageType) → params, with the recommended LoRA shown (resolved by the
   backend) and the auto-sort destination previewed — all BEFORE submitting.

   Adapted faithfully from the design screen (design/screens/skapa.jsx) but:
     • wired to the live backend, not the mock window.LB:
         - GET  /api/taxonomy        — subjects/areas/imageTypes (the SoT, lb-582)
         - POST /api/resolve-lora     — the authoritative LoRA for (subject,
                                        imageType) (lb-9tz); shown, never editable.
     • the face-swap ("Ansikte") section is intentionally OMITTED. That flow is
       consent-gated and lives under the Porträtt view (P7); it has no place in
       the strictly-SFW image funnel, and its keywords would trip the SFW marker
       guard that scans this very file (see tests/test_skapa_view.py).

   Pure funnel/payload logic lives in skapa_data.js (window.SkapaData), unit-
   tested under node. This file is the React rendering + live-fetch layer. No
   build step — Babel-in-browser, matching the design's serving model. Reuses
   the design-system component classes from static/css (split, section-h,
   area-rail, opt-grid/opt, suggest, field, range, panel, chip, btn) — no inline
   colour/spacing beyond the documented per-subject hue (--brand-h) mechanism.

   The submit (POST /api/jobs) and live SSE progress are layered on in the
   stacked branches lb-5wp (P3.2) and lb-b1r (P3.3); this branch establishes the
   funnel + the resolved-LoRA preview the user reviews before submitting.
   SFW-only: educational image funnel; no restricted feature, model, or marker.
   ============================================================================ */
const { useState, useEffect, useRef } = React;
const S = window.SkapaData;

/* ---------- minimal icon set (subset of the design's ui.jsx I) ---------- */
const SkIco = ({ d, size = 18, sw = 1.6, fill = "none" }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill={fill} stroke="currentColor"
    strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round">{d}</svg>
);
const SI = {
  sparkle: <SkIco d={<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z" />} />,
  library: <SkIco d={<><path d="M4 5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z" /><path d="M13 3v5h5" /></>} />,
  arrow:   <SkIco d={<path d="M5 12h14M13 6l6 6-6 6" />} sw={2} />,
};

/* ---------- small presentational helpers (match the CSS component classes) -- */
function SectionH({ children, acc }) {
  return (
    <div className="section-h">
      <span>{children}</span>
      {acc != null && <span className="acc">{acc}</span>}
    </div>
  );
}

/* one selectable bildtyp tile — renders to the real .opt structure (.ot/.g/.od) */
function TypeOpt({ t, active, onClick }) {
  return (
    <button className={`opt ${active ? "active" : ""}`} onClick={onClick} title={t.desc} type="button">
      <div className="ot"><span className="g">{t.glyph}</span>{t.name}</div>
      <div className="od">{t.desc}</div>
    </button>
  );
}

/* the resolved-LoRA callout (.suggest) — backend-authoritative, read-only */
function LoraSuggest({ state, subject, imageType }) {
  // state: { loading } | { error } | { lora: {name, strength, source, note} }
  const sourceLabel = (src) =>
    src === "ämnesspecifik" ? "Ämnesspecifik LoRA" : "Bas-LoRA för bildtypen";
  if (state.loading) {
    return (
      <div className="suggest">
        <div className="sl">Rekommenderad LoRA</div>
        <div className="sv dim">Slår upp…</div>
      </div>
    );
  }
  if (state.error || !state.lora) {
    return (
      <div className="suggest">
        <div className="sl">Rekommenderad LoRA</div>
        <div className="sv dim">— kunde inte slå upp</div>
        {state.error && <div className="sn mono">{state.error}</div>}
      </div>
    );
  }
  const lora = state.lora;
  return (
    <div className="suggest">
      <div className="sl">{sourceLabel(lora.source)}</div>
      <div className="sv">{SI.sparkle} {lora.name}</div>
      {lora.note && <div className="sn">{lora.note}</div>}
      <div className="row" style={{ marginTop: 10 }}>
        <span className="mono dim grow">styrka {lora.strength}</span>
        <span className="mono" style={{ color: "var(--brand-ink)" }}>väljs av servern</span>
      </div>
    </div>
  );
}

/* ---------- the Skapa funnel view ---------- */
function Skapa() {
  // taxonomy bootstrap (the SoT)
  const [tax, setTax] = useState(null);
  const [bootError, setBootError] = useState(null);

  // funnel selection
  const [subjectSlug, setSubjectSlug] = useState(null);
  const [areaSlug, setAreaSlug] = useState(null);
  const [typeSlug, setTypeSlug] = useState(null);
  const [prompt, setPrompt] = useState("");

  // params
  const [seed, setSeed] = useState("");
  const [count, setCount] = useState(1);

  // resolved LoRA (backend-authoritative) — { loading } | { error } | { lora }
  const [loraState, setLoraState] = useState({ loading: false });

  // ---- bootstrap the taxonomy ----
  useEffect(() => {
    let alive = true;
    fetch("/api/taxonomy")
      .then((r) => { if (!r.ok) throw new Error(`/api/taxonomy ${r.status}`); return r.json(); })
      .then((data) => {
        if (!alive) return;
        setTax(data);
        // Default to the first science subject + first image type so the funnel
        // opens on a meaningful starting state (area is left unchosen on purpose
        // — picking the area is the teacher's first deliberate funnel step).
        const ordered = S.orderSubjects(data.subjects || []);
        if (ordered.length) setSubjectSlug(ordered[0].slug);
        if ((data.imageTypes || []).length) setTypeSlug(data.imageTypes[0].slug);
      })
      .catch((e) => { if (alive) setBootError(String(e)); });
    return () => { alive = false; };
  }, []);

  // resolved nodes from the current selection
  const subjects = tax ? tax.subjects || [] : [];
  const imageTypes = tax ? tax.imageTypes || [] : [];
  const subject = S.findSubject(subjects, subjectSlug);
  const area = S.findArea(subject, areaSlug);
  const typeObj = S.findImageType(imageTypes, typeSlug);

  // apply the active subject's hue to the page accent (per-view --brand-h)
  const hue = S.subjectHue(subject);
  useEffect(() => {
    document.documentElement.style.setProperty("--brand-h", hue);
  }, [hue]);

  // reset the area whenever the subject changes (areas are subject-scoped, so a
  // stale area slug would be invalid for the new subject)
  useEffect(() => { setAreaSlug(null); }, [subjectSlug]);

  // ---- resolve the LoRA whenever (subject, imageType) changes ----
  // POST /api/resolve-lora is the single authority; the funnel only displays the
  // result and never lets the user choose a non-allowlisted LoRA (there is no
  // editable LoRA control at all — by construction the client cannot override).
  useEffect(() => {
    if (!subjectSlug || !typeSlug) { setLoraState({ loading: false }); return; }
    let alive = true;
    setLoraState({ loading: true });
    fetch("/api/resolve-lora", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subject: subjectSlug, imageType: typeSlug }),
    })
      .then((r) => r.json().then((j) => ({ ok: r.ok, j })))
      .then(({ ok, j }) => {
        if (!alive) return;
        if (!ok) { setLoraState({ error: j.error || "uppslag misslyckades" }); return; }
        setLoraState({ lora: j.lora });
      })
      .catch((e) => { if (alive) setLoraState({ error: String(e) }); });
    return () => { alive = false; };
  }, [subjectSlug, typeSlug]);

  // submit readiness (mirror of the server's required-field check)
  const ready = S.submitReadiness({ subject, area, imageType: typeObj, prompt });
  const dest = S.destinationLabel(subject, area);

  // ---- submit hook: filled in by lb-5wp (POST /api/jobs) + lb-b1r (SSE). ----
  // On this branch the funnel is complete through "ready to submit"; clicking
  // Generera is a no-op placeholder so the funnel renders + behaves without the
  // job pipeline yet. The stacked branches replace onGenerate.
  const onGenerate = () => { /* lb-5wp: POST /api/jobs with S.buildJobPayload(...) */ };

  // ---------- boot states ----------
  if (bootError) {
    return (
      <div className="page" data-screen-label="Skapa bild">
        <div className="page-body">
          <div className="empty">
            <div className="pull">Kunde inte läsa taxonomin.</div>
            <div className="dim mono">{bootError}</div>
          </div>
        </div>
      </div>
    );
  }
  if (!tax || !subject || !typeObj) {
    return (
      <div className="page" data-screen-label="Skapa bild">
        <div className="page-body"><div className="empty"><div className="dim">Läser in…</div></div></div>
      </div>
    );
  }

  const orderedSubjects = S.orderSubjects(subjects);
  const placeholderPrompt = area
    ? `t.ex. ${area.central.split(",")[0].toLowerCase()}`
    : `t.ex. ${subject.blurb ? subject.blurb.toLowerCase() : "beskriv motivet"}`;

  return (
    <div className="page" data-screen-label="Skapa bild">
      <div className="page-head">
        <div>
          <h1 className="serif">Skapa <em>bild</em></h1>
          <div className="caption">Välj ämne och område · rätt LoRA föreslås automatiskt · bilden sorteras in i biblioteket</div>
        </div>
        <div className="page-head-actions">
          <span className="gpu-tag">auto · vm100 3090</span>
        </div>
      </div>

      <div className="split" style={{ gridTemplateColumns: "300px 1fr 320px" }}>
        {/* LEFT — ämne / område / bildtyp */}
        <div className="split-left">
          <SectionH>Ämne</SectionH>
          <div className="field">
            <select value={subjectSlug} onChange={(e) => setSubjectSlug(e.target.value)}>
              {orderedSubjects.map((s) => <option key={s.slug} value={s.slug}>{s.name}</option>)}
            </select>
          </div>

          <div style={{ height: 18 }} />
          <SectionH acc={subject.areas.length}>Område</SectionH>
          <div className="area-rail" style={{ maxHeight: 230, overflowY: "auto" }}>
            {subject.areas.map((a) => (
              <button key={a.slug} type="button"
                className={`area-item ${areaSlug === a.slug ? "active" : ""}`}
                onClick={() => setAreaSlug(a.slug)} title={a.central}>
                <span className="ag">{a.glyph}</span>
                <span className="an">{a.name}</span>
              </button>
            ))}
          </div>

          <div style={{ height: 18 }} />
          <SectionH>Bildtyp</SectionH>
          <div className="opt-grid">
            {imageTypes.map((t) => (
              <TypeOpt key={t.slug} t={t} active={typeSlug === t.slug} onClick={() => setTypeSlug(t.slug)} />
            ))}
          </div>
        </div>

        {/* CENTER — context chips + prompt */}
        <div className="split-center">
          <div className="row" style={{ marginBottom: 14 }}>
            <span className="chip accent plain">
              <span className="subj-dot" style={{ background: S.subjColor(subject), width: 8, height: 8, borderRadius: 2 }} />
              {subject.name}
            </span>
            {area && <span className="chip plain">{area.glyph} {area.name}</span>}
            <span className="chip plain">{typeObj.glyph} {typeObj.name}</span>
            <span className="grow" />
            <span className="mono dim">{count > 1 ? `${count} bilder` : "1 bild"}</span>
          </div>

          <div className="canvas-frame">
            <div className="empty" style={{ padding: 32 }}>
              <div className="pull" style={{ fontSize: 18 }}>{(area ? area.name : subject.name)}</div>
              <div className="dim mono" style={{ marginTop: 6 }}>{subject.short} · {typeObj.name.toUpperCase()}</div>
            </div>
          </div>

          <div className="panel" style={{ marginTop: 16 }}>
            <div className="panel-body" style={{ padding: 14 }}>
              <div className="field">
                <label>Beskrivning av bilden</label>
                <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder={placeholderPrompt} rows={3} />
              </div>
              <div className="row" style={{ marginTop: 12 }}>
                <span className="mono dim grow">Negativ prompt fylls på automatiskt för {typeObj.name.toLowerCase()}</span>
              </div>
            </div>
          </div>
        </div>

        {/* RIGHT — resolved LoRA + destination + params + generate */}
        <div className="split-right">
          <SectionH>Rekommenderad LoRA</SectionH>
          <LoraSuggest state={loraState} subject={subjectSlug} imageType={typeSlug} />

          <div className="divider" />

          <SectionH>Sorteras in i</SectionH>
          <div className="panel" style={{ padding: "12px 14px", display: "flex", alignItems: "center", gap: 10 }}>
            <span className="ico" style={{ color: "var(--brand)" }}>{SI.library}</span>
            <div>
              <div style={{ fontWeight: 600, fontSize: 13 }}>{dest}</div>
              <div className="mono dim" style={{ fontSize: 10 }}>{area ? "automatiskt — sorteras hit" : "välj ett område till vänster"}</div>
            </div>
          </div>

          <div className="divider" />

          <SectionH>Inställningar</SectionH>
          <div className="stack">
            <div className="field">
              <label>Antal <span className="val">{count}</span></label>
              <input className="range" type="range" min="1" max="8" value={count}
                onChange={(e) => setCount(+e.target.value)} />
            </div>
            <div className="field">
              <label>Seed <span className="val">{seed === "" ? "slumpas" : ""}</span></label>
              <input type="number" min="0" value={seed} placeholder="slumpmässig"
                onChange={(e) => setSeed(e.target.value)} />
            </div>
          </div>

          <button type="button" className="btn primary"
            style={{ width: "100%", justifyContent: "center", padding: "13px 0", fontSize: 15, marginTop: 18 }}
            disabled={!ready.ok} onClick={onGenerate}>
            {SI.sparkle} {ready.ok ? (count > 1 ? `Generera ${count} bilder` : "Generera 1 bild") : `Generera (${ready.reason})`}
          </button>
        </div>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<Skapa />);
