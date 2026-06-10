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

   Submit (POST /api/jobs, lb-5wp / P3.2): on Generera the funnel builds the job
   payload via SkapaData.buildJobPayload (no client LoRA — the worker resolves +
   allowlist-validates it server-side), POSTs it, and captures the returned job
   id. A 4xx/429 is surfaced as a clear Swedish message; the button is locked
   while a submit is in flight.

   Live progress (SSE, lb-b1r / P3.3): once a job is accepted the funnel opens an
   EventSource to GET /api/jobs/<id>/events and renders queued → running (with the
   step/total progress) → done. On done it shows the result (dimensions) and a
   link to where the worker auto-sorted the asset in the Bibliotek (by subject/
   area). The SSE endpoint resumes the current state on connect, so a reload mid-
   render catches up. SFW-only: educational image funnel; no restricted marker.
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

/* a slim progress bar (0..100, or indeterminate when percent is null) built from
   the design tokens — no new colours, just the brand accent on a sunk track. */
function ProgressBar({ percent }) {
  const indeterminate = percent == null;
  return (
    <div style={{ height: 6, borderRadius: 3, background: "var(--sink)", overflow: "hidden", marginTop: 8 }}>
      <div style={{
        height: "100%",
        width: indeterminate ? "35%" : `${percent}%`,
        background: "var(--brand)",
        borderRadius: 3,
        transition: "width 200ms",
        opacity: indeterminate ? 0.6 : 1,
      }} />
    </div>
  );
}

/* submit + live status (lb-5wp submit, lb-b1r progress): the POST /api/jobs
   result, then the live SSE render progress (queued → running → done). On done it
   shows the result dimensions and a link to where the asset auto-sorted in the
   Bibliotek (by subject/area). */
function SubmitStatus({ submit, jobState, onReset }) {
  if (submit.phase === "idle") return null;

  if (submit.phase === "submitting") {
    return (
      <div className="panel" style={{ padding: "12px 14px", marginTop: 12 }}>
        <div className="mono dim" style={{ fontSize: 11 }}>Skickar…</div>
      </div>
    );
  }

  if (submit.phase === "error") {
    return (
      <div className="panel" style={{ padding: "12px 14px", marginTop: 12, borderColor: "var(--danger)" }}>
        <div className="upper" style={{ color: "var(--danger)", marginBottom: 6 }}>Kunde inte skapa</div>
        <div style={{ fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.4 }}>{submit.message}</div>
        <button type="button" className="btn sm ghost" style={{ marginTop: 10 }} onClick={onReset}>Försök igen</button>
      </div>
    );
  }

  // submitted (202): the job is accepted. Until the first SSE event lands we know
  // only that it is queued; describeJobState then drives the live view.
  const d = jobState || S.describeJobState({ status: "queued" });
  const dest = submit.dest || {};
  // lb-9d5: when N>1 images were queued, the bar tails the first job; say so.
  const count = submit.count || 1;
  const batchNote = count > 1
    ? <div style={{ fontSize: 11.5, color: "var(--ink-2)", marginTop: 6 }}>
        {count} bilder köade — förloppet visar den första; resten dyker upp i biblioteket när de är klara.
      </div>
    : null;

  // ---- terminal: failed ----
  if (d.failed) {
    return (
      <div className="panel" style={{ padding: "12px 14px", marginTop: 12, borderColor: "var(--danger)" }}>
        <div className="upper" style={{ color: "var(--danger)", marginBottom: 6 }}>Renderingen misslyckades</div>
        {d.error && <div style={{ fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.4 }}>{d.error}</div>}
        <div className="mono dim" style={{ fontSize: 10, marginTop: 6 }}>jobb {submit.jobId}</div>
        <button type="button" className="btn sm ghost" style={{ marginTop: 10 }} onClick={onReset}>Försök igen</button>
      </div>
    );
  }

  // ---- terminal: done → result + Bibliotek deep-link ----
  if (d.done) {
    const link = S.bibliotekLink(dest.subject, dest.area);
    const dims = (d.width && d.height) ? `${d.width}×${d.height}` : null;
    return (
      <div className="panel" style={{ padding: "12px 14px", marginTop: 12, borderColor: "var(--good)" }}>
        <div className="upper" style={{ color: "var(--good)", marginBottom: 6 }}>Klar</div>
        <div className="row">
          <span className="mono dim grow" style={{ fontSize: 10 }}>jobb {submit.jobId}</span>
          {dims && <span className="mono dim" style={{ fontSize: 10 }}>{dims}</span>}
        </div>
        <div style={{ fontSize: 12.5, color: "var(--ink-2)", marginTop: 8, lineHeight: 1.4 }}>
          {count > 1
            ? `Den första bilden är klar och sorterad i biblioteket; de övriga ${count - 1} renderas och dyker upp där.`
            : "Bilden sorterades in i biblioteket."}
        </div>
        <div className="row" style={{ marginTop: 10, gap: 8 }}>
          <a className="btn sm" href={link}>{SI.library} Visa i biblioteket</a>
          <button type="button" className="btn sm ghost" onClick={onReset}>Skapa en till</button>
        </div>
      </div>
    );
  }

  // ---- in flight: queued / running with the live progress bar ----
  return (
    <div className="panel" style={{ padding: "12px 14px", marginTop: 12 }}>
      <div className="row">
        <span className="upper grow" style={{ color: "var(--brand-ink)" }}>{d.label}</span>
        {d.percent != null && <span className="mono dim" style={{ fontSize: 10 }}>{d.percent}%</span>}
      </div>
      <ProgressBar percent={d.percent} />
      <div className="mono dim" style={{ fontSize: 10, marginTop: 8 }}>jobb {submit.jobId}</div>
      {batchNote}
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

  // A deep-linked area (from ?area=, lb-skapafix) waiting to survive the
  // subject→area reset that the initial setSubjectSlug triggers. Consumed once by
  // the reset effect; null in the normal (user-driven) case.
  const pendingAreaRef = useRef(null);

  // params
  const [seed, setSeed] = useState("");
  const [count, setCount] = useState(1);

  // resolved LoRA (backend-authoritative) — { loading } | { error } | { lora }
  const [loraState, setLoraState] = useState({ loading: false });

  // submit lifecycle (lb-5wp): the funnel's POST /api/jobs round-trip.
  //   { phase: "idle" }
  //   { phase: "submitting" }
  //   { phase: "submitted", jobId, dest:{subject,area} }   ← accepted (202)
  //   { phase: "error", message }     ← 4xx/429/503, a Swedish sentence to show
  // The destination slugs are captured AT SUBMIT TIME so the live status + the
  // Bibliotek deep-link stay correct even if the funnel selection changes after.
  const [submit, setSubmit] = useState({ phase: "idle" });

  // live job state from the SSE stream (lb-b1r): the latest describeJobState(...)
  // result, or null before the first event. Drives the progress bar + done view.
  const [jobState, setJobState] = useState(null);

  // ---- bootstrap the taxonomy ----
  useEffect(() => {
    let alive = true;
    fetch("/api/taxonomy")
      .then((r) => { if (!r.ok) throw new Error(`/api/taxonomy ${r.status}`); return r.json(); })
      .then((data) => {
        if (!alive) return;
        setTax(data);
        // Honour a pre-scoped selection deep-linked from the Bibliotek "Skapa
        // här" buttons (?subject=&area=, lb-skapafix) — validated against the
        // taxonomy so a stale/bogus URL can't select an invalid node. When a
        // param is absent/unknown we fall back to the first science subject (and
        // leave the area unchosen, so picking it stays the teacher's deliberate
        // funnel step). The image type always defaults to the first type.
        const ordered = S.orderSubjects(data.subjects || []);
        const ctx = S.readSelectionFromQuery(
          typeof window !== "undefined" ? window.location.search : "", data);
        if (ctx.subjectSlug) {
          // Stash the deep-linked area so the subject→area reset effect adopts it
          // instead of nulling it (the reset is meant for *user* subject changes,
          // not this initial deep-link). See the reset effect below.
          pendingAreaRef.current = ctx.areaSlug || null;
          setSubjectSlug(ctx.subjectSlug);
        } else if (ordered.length) {
          setSubjectSlug(ordered[0].slug);
        }
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
  // stale area slug would be invalid for the new subject) — EXCEPT when a deep-
  // linked area is pending for this exact subject set (lb-skapafix): then adopt
  // it instead of nulling, so "Skapa här" from inside an area lands pre-scoped.
  useEffect(() => {
    const pending = pendingAreaRef.current;
    pendingAreaRef.current = null; // consume once; later user changes still reset
    setAreaSlug(pending);
  }, [subjectSlug]);

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

  // ---- submit: POST /api/jobs (lb-5wp) ----
  // Build the payload from the current selection (slugs + params; never a LoRA),
  // POST it, and capture the job id. The backend re-validates the whole
  // selection (and resolves/allowlist-checks the LoRA itself); a 4xx/429 comes
  // back as a structured error we turn into one Swedish sentence. The button is
  // locked while a submit is in flight so a double-click can't enqueue twice.
  const onGenerate = () => {
    if (!ready.ok || submit.phase === "submitting") return;
    const payload = S.buildJobPayload({
      subject: subjectSlug,
      area: areaSlug,
      imageType: typeSlug,
      prompt,
      seed,
      count,
    });
    const dest = { subject: subjectSlug, area: areaSlug };
    setSubmit({ phase: "submitting" });
    setJobState(null);
    fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then((r) => r.json().catch(() => null).then((j) => ({ status: r.status, body: j })))
      .then(({ status, body }) => {
        const res = S.interpretSubmitResponse(status, body);
        if (res.ok) setSubmit({ phase: "submitted", jobId: res.jobId, count: res.count || 1, dest });
        else setSubmit({ phase: "error", message: res.message });
      })
      .catch((e) => setSubmit({ phase: "error", message: `Nätverksfel: ${e}` }));
  };

  // A fresh render: clear the submit + live state so the funnel returns to form.
  const onReset = () => { setSubmit({ phase: "idle" }); setJobState(null); };

  // ---- live progress: tail the job's SSE stream while one is accepted (lb-b1r) ----
  // Open an EventSource once a job is accepted; each `data:` payload is a job-
  // state blob (the worker contract). We normalise it via describeJobState and
  // close on a terminal status (the server also sends `event: end`). The effect
  // re-runs per jobId and always tears the stream down on cleanup/unmount, so a
  // new submit or a reset never leaks a connection. EventSource auto-reconnects
  // on a dropped connection, and the endpoint resumes the current state on
  // connect — so a blip catches up rather than stalling.
  const jobId = submit.phase === "submitted" ? submit.jobId : null;
  useEffect(() => {
    if (!jobId) return undefined;
    if (typeof EventSource === "undefined") return undefined; // SSR/old browser guard
    const es = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`);
    const onMessage = (ev) => {
      let parsed;
      try { parsed = JSON.parse(ev.data); } catch (e) { return; } // ignore non-JSON keepalives
      const d = S.describeJobState(parsed);
      setJobState(d);
      if (d.terminal) es.close(); // no more updates after done/failed/cancelled
    };
    es.addEventListener("message", onMessage);
    es.addEventListener("end", () => es.close());
    return () => es.close();
  }, [jobId]);

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
            disabled={!ready.ok || submit.phase === "submitting"} onClick={onGenerate}>
            {SI.sparkle} {submit.phase === "submitting"
              ? "Skickar…"
              : ready.ok
                ? (count > 1 ? `Generera ${count} bilder` : "Generera 1 bild")
                : `Generera (${ready.reason})`}
          </button>

          <SubmitStatus submit={submit} jobState={jobState} onReset={onReset} />
        </div>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<Skapa />);
