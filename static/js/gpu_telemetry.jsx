/* ============================================================================
   Lärbild — GPU telemetry pills + Kö (queue) view  (lb-dop)
   ----------------------------------------------------------------------------
   The frontend consumers for the live GET /api/gpus + /api/gpus/stream
   endpoints (backend lb-c2r, app/gpu_telemetry.py). Mirrors the imagegen
   Atelier pattern faithfully (sibbis-app/static/atelier/ui/hooks.jsx
   useGpuTelemetry, shell.jsx GPU pills, screens/queue.jsx telemetry cards) —
   forked, not reinvented, and trimmed to Lärbild's surface:

     • useGpuTelemetry() — ONE singleton EventSource on /api/gpus/stream shared
       by the pills + the Kö modal, with a one-shot /api/gpus fallback when the
       browser has no EventSource. Identical lifecycle to the atelier hook (the
       connection is left open across mount/unmount so nav doesn't thrash the
       socket / double the SSH poll load).
     • GpuPills — the two top-bar pills (vm100 · 3090, vm153 · B70) showing live
       util; clicking either opens the Kö modal.
     • KoModal — the queue/telemetry view: per-GPU cards (util sparkline,
       VRAM/power/temp bars, queue depth + running job). Lärbild has no
       /api/queue/list per-job table endpoint, so the card surfaces the queue
       depth + current job id the snapshot already carries — no invented route.

   These are owner-only (hidden on the public read-only bilder.sibbamala.com
   instance — the caller gates them on PUBLIC_READONLY). Exposed on window so the
   Babel-in-browser bibliotek.jsx (a separate <script>) can use them.
   SFW-only: pure GPU stat display; no restricted feature.
   ============================================================================ */
(function () {
  const { useState, useEffect, useRef } = React;

  /* ---- snapshot shape (from app/gpu_telemetry.snapshot) ----
     {vm100: {util, vram_used_mb, vram_total_mb, power_w, temp_c, queue_depth,
              current_job_id, stale, last_update, error},
      vm153: {... same ...}, ts: float}                                       */

  // Singleton SSE consumer for /api/gpus/stream. One EventSource per tab — the
  // pills + the Kö modal both subscribe to the same stream rather than opening N
  // connections. The connection is left open across mount/unmount; if the
  // browser lacks EventSource we fall back to polling /api/gpus once per second.
  function useGpuTelemetry() {
    const [snapshot, setSnapshot] = useState(() => {
      if (typeof window === "undefined") return null;
      return (window.__lbGpuTelemetry && window.__lbGpuTelemetry.last) || null;
    });
    useEffect(() => {
      if (typeof window === "undefined") return;
      if (!window.__lbGpuTelemetry) {
        window.__lbGpuTelemetry = { es: null, poll: null, subscribers: new Set(), last: null };
      }
      const hub = window.__lbGpuTelemetry;
      hub.subscribers.add(setSnapshot);
      if (hub.last) setSnapshot(hub.last);

      const push = (data) => {
        hub.last = data;
        for (const sub of hub.subscribers) sub(data);
      };

      if (!hub.es && !hub.poll) {
        // One-shot prime so the pills/cards show real numbers immediately,
        // before the SSE stream's first event arrives (or if it never does).
        if (!hub.last) {
          fetch("/api/gpus")
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => { if (d) push(d); })
            .catch(() => { /* transient — the stream/poll will catch up */ });
        }

        const startPolling = () => {
          if (hub.poll) return; // already polling
          const tick = () => {
            fetch("/api/gpus")
              .then((r) => (r.ok ? r.json() : null))
              .then((d) => { if (d) push(d); })
              .catch(() => { /* transient — keep last good */ });
          };
          tick();
          hub.poll = setInterval(tick, 1000);
        };

        if (typeof EventSource !== "undefined") {
          try {
            hub.es = new EventSource("/api/gpus/stream");
            hub.es.onmessage = (e) => {
              try { push(JSON.parse(e.data)); } catch (_e) { /* keep last good */ }
            };
            // A 404/500/proxy-buffer failure surfaces here (it does NOT throw at
            // construction). The browser keeps auto-reconnecting, but if the
            // stream stays down we still want live numbers — so fall back to the
            // one-shot poll. Polling is idempotent (startPolling guards) and the
            // poll + a later-recovering stream both just call push().
            hub.es.onerror = () => startPolling();
          } catch (_e) {
            hub.es = null;
          }
        }
        if (!hub.es) {
          // No EventSource at all (old browser) — poll the snapshot endpoint.
          startPolling();
        }
      }
      return () => {
        hub.subscribers.delete(setSnapshot);
        // Connection/poll stay open — re-opening on every nav round-trip would
        // restart the SSE stream and double the SSH poll load on the hosts.
      };
    }, []);
    return snapshot;
  }

  // Pull one GPU's view-model out of a snapshot (null-safe).
  function gpuView(snap, key) {
    const g = snap && snap[key];
    const stale = !g || g.stale;
    const util = g ? Math.round(Number(g.util) || 0) : null;
    return { g, stale, util };
  }

  /* ---------- top-bar pills ---------- */
  // Two pills, one per host. Live util%, "—" when stale; clicking opens the Kö
  // modal. `warn` class when util is hot, `idle` when stale (mirrors atelier).
  function GpuPills({ onOpenQueue }) {
    const telem = useGpuTelemetry();
    const vm100 = gpuView(telem, "vm100");
    const vm153 = gpuView(telem, "vm153");

    const v100Cls = vm100.util != null && vm100.util > 85 ? "warn" : (vm100.stale ? "idle" : "");
    const v153Cls = vm153.stale ? "idle" : "";
    const v100Title = vm100.stale
      ? "vm100 telemetri otillgänglig — SSH/nvidia-smi nere"
      : `vm100 · ${(vm100.g.queue_depth || 0)} i kö${vm100.g.current_job_id ? " · kör" : ""}`;
    const v153Title = vm153.stale
      ? "vm153 / B70 telemetri inaktuell (intel_gpu_top stöder kanske inte Xe-drivrutinen)"
      : `vm153 · B70 · ${vm153.util}%`;

    return (
      <div className="gpu-pills">
        <button type="button" className={`gpu-pill ${v100Cls}`} title={v100Title}
          onClick={onOpenQueue} data-vm="vm100">
          <span className="dot" />vm100 · 3090 · {vm100.util != null && !vm100.stale ? `${vm100.util}%` : "—"}
        </button>
        <button type="button" className={`gpu-pill ${v153Cls}`} title={v153Title}
          onClick={onOpenQueue} data-vm="vm153">
          <span className="dot" />vm153 · B70 · {vm153.util != null && !vm153.stale ? `${vm153.util}%` : "—"}
        </button>
      </div>
    );
  }

  /* ---------- helpers (pure, mirror atelier queue.jsx) ---------- */
  function pct(used, total) {
    const u = Number(used) || 0, t = Number(total) || 0;
    if (t <= 0) return 0;
    return Math.max(0, Math.min(100, (u / t) * 100));
  }
  function powerPct(w, cap) { return Math.max(0, Math.min(100, ((Number(w) || 0) / cap) * 100)); }
  function tempPct(c, cap) { return Math.max(0, Math.min(100, ((Number(c) || 0) / cap) * 100)); }
  function shortId(id) { return id ? String(id).slice(0, 8) : "—"; }

  // Rolling util history for the sparkline (one sample per snapshot, ~1/sec).
  function useUtilHistory(util, stale, len = 48) {
    const [history, setHistory] = useState(() => Array(len).fill(0));
    useEffect(() => {
      if (stale) return;
      const v = Math.max(0, Math.min(100, Number(util) || 0));
      setHistory((prev) => [...prev.slice(-(len - 1)), v]);
    }, [util, stale, len]);
    return history;
  }

  // Tiny inline SVG sparkline (no chart dependency — stdlib SVG).
  function Sparkline({ values, height = 38 }) {
    const w = 220, n = values.length;
    if (n === 0) return <svg width="100%" height={height} viewBox={`0 0 ${w} ${height}`} preserveAspectRatio="none" />;
    const step = w / Math.max(1, n - 1);
    const pts = values.map((v, i) => `${(i * step).toFixed(1)},${(height - (v / 100) * height).toFixed(1)}`).join(" ");
    return (
      <svg className="spark" width="100%" height={height} viewBox={`0 0 ${w} ${height}`} preserveAspectRatio="none">
        <polyline points={pts} fill="none" stroke="var(--brand)" strokeWidth="1.6"
          strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
      </svg>
    );
  }

  /* ---------- one GPU telemetry card ---------- */
  function GpuCard({ name, sub, g, stale, util }) {
    const series = useUtilHistory(util, stale);
    const vramUsed = g ? Number(g.vram_used_mb) || 0 : 0;
    const vramTotal = g ? Number(g.vram_total_mb) || 0 : 0;
    const vramPct = pct(vramUsed, vramTotal);
    const power = g ? Number(g.power_w) || 0 : 0;
    const temp = g ? Number(g.temp_c) || 0 : 0;
    const queueDepth = g ? Number(g.queue_depth) || 0 : 0;
    const runningJobId = g && g.current_job_id;

    return (
      <div className="tele-card" data-vm={name}>
        <div className="tele-head">
          <div>
            <div className="tele-name serif">{name}</div>
            <div className="tele-sub mono">{sub}</div>
          </div>
          <span className={`tele-chip ${stale ? "off" : runningJobId ? "run" : "idle"}`}>
            {stale ? "offline" : runningJobId ? "kör" : "ledig"}
          </span>
        </div>

        <div className="tele-util">
          <div className="tele-row">
            <span className="l mono upper">GPU-belastning</span>
            <span className="v mono">{stale ? "—" : `${util}%`}</span>
          </div>
          <Sparkline values={series} />
        </div>

        <div className="tele-bars">
          <div>
            <div className="tele-row"><span className="l">VRAM</span>
              <span className="v mono">{stale || vramTotal <= 0 ? "—" : `${(vramUsed / 1024).toFixed(1)} / ${(vramTotal / 1024).toFixed(0)} GB`}</span></div>
            <div className="bar"><div className="bar-fill" style={{ width: `${stale ? 0 : vramPct}%`, background: "var(--brand)" }} /></div>
          </div>
          <div>
            <div className="tele-row"><span className="l">Effekt</span>
              <span className="v mono">{stale || power <= 0 ? "—" : `${Math.round(power)} W`}</span></div>
            <div className="bar"><div className="bar-fill" style={{ width: `${stale ? 0 : powerPct(power, 450)}%`, background: "var(--good)" }} /></div>
          </div>
          <div>
            <div className="tele-row"><span className="l">Temp</span>
              <span className="v mono">{stale || !temp ? "—" : `${temp}°C`}</span></div>
            <div className="bar"><div className="bar-fill" style={{ width: `${stale ? 0 : tempPct(temp, 95)}%`, background: temp > 80 ? "var(--warn)" : "var(--good)" }} /></div>
          </div>
          <div>
            <div className="tele-row"><span className="l">Kö</span>
              <span className="v mono">{runningJobId ? 1 : 0} kör · {queueDepth} väntar</span></div>
            <div className="tele-job mono dim">{runningJobId ? shortId(runningJobId) : "—"}</div>
          </div>
        </div>
      </div>
    );
  }

  /* ---------- job management (lb-07y) ---------- */
  // Owner-only render-queue actions, surfaced inside the Kö modal: list the
  // recent jobs and cancel a queued/running one or retry a failed/cancelled one.
  // Backed by the OWNER-ONLY endpoints (the jobs blueprint isn't registered on
  // the public read-only instance, and the modal itself never mounts there):
  //   GET  /api/jobs?status=&limit=   POST /api/jobs/<id>/cancel   /retry
  // Polls the list on a slow tick so an action's effect (and the worker's own
  // transitions) show up without a manual reload.
  function useJobList(pollMs = 3000) {
    const [jobs, setJobs] = useState(null);   // null = loading; [] = empty
    const [error, setError] = useState(null);
    const reload = React.useCallback(() => {
      return fetch("/api/jobs?limit=60")
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((d) => { setJobs(d.jobs || []); setError(null); })
        .catch((e) => { setError(String(e.message || e)); });
    }, []);
    useEffect(() => {
      let alive = true;
      const tick = () => { if (alive) reload(); };
      tick();
      const id = setInterval(tick, pollMs);
      return () => { alive = false; clearInterval(id); };
    }, [reload, pollMs]);
    return { jobs, error, reload };
  }

  const ACTIVE_STATUS = { queued: 1, running: 1 };       // cancellable
  const RETRYABLE_STATUS = { failed: 1, cancelled: 1 };  // retryable

  function statusLabel(s) {
    return ({ queued: "i kö", running: "kör", done: "klar",
              failed: "misslyckades", cancelled: "avbruten" })[s] || s;
  }

  // One row in the job-management table. Shows the job's subject/area/type +
  // status, with a Cancel (queued/running) or Retry (failed/cancelled) action.
  function JobRow({ job, busy, onCancel, onRetry }) {
    const s = job.status;
    const typ = job.image_type || job.video_type || job.kind;
    return (
      <div className="ko-job" data-status={s}>
        <div className="ko-job-main">
          <span className={`ko-job-dot ${s}`} />
          <div className="ko-job-text">
            <div className="ko-job-title">{job.title || `${job.subject} · ${job.area}`}</div>
            <div className="ko-job-meta mono dim">{typ} · {shortId(job.id)}
              {job.error ? ` · ${String(job.error).slice(0, 60)}` : ""}</div>
          </div>
        </div>
        <div className="ko-job-side">
          <span className={`ko-job-status ${s}`}>{statusLabel(s)}</span>
          {ACTIVE_STATUS[s] && (
            <button type="button" className="ko-act cancel" disabled={busy}
              onClick={() => onCancel(job.id)} title="Avbryt jobbet">Avbryt</button>
          )}
          {RETRYABLE_STATUS[s] && (
            <button type="button" className="ko-act retry" disabled={busy}
              onClick={() => onRetry(job.id)} title="Köa om jobbet">Försök igen</button>
          )}
        </div>
      </div>
    );
  }

  // The job-management list inside the Kö modal: live job rows + cancel/retry.
  function JobQueueList() {
    const { jobs, error, reload } = useJobList();
    const [busyId, setBusyId] = useState(null);
    const [actErr, setActErr] = useState(null);

    const act = (id, path) => {
      setBusyId(id); setActErr(null);
      fetch(path, { method: "POST" })
        .then((r) => (r.ok ? r.json()
          : r.json().catch(() => ({})).then((b) => Promise.reject(new Error(b.error || `HTTP ${r.status}`)))))
        .then(() => reload())
        .catch((e) => setActErr(String(e.message || e)))
        .finally(() => setBusyId(null));
    };
    const onCancel = (id) => act(id, `/api/jobs/${id}/cancel`);
    const onRetry = (id) => act(id, `/api/jobs/${id}/retry`);

    // Surface the actionable + recent jobs first (active, then the rest).
    const ordered = (jobs || []).slice().sort((a, b) => {
      const rank = (j) => (ACTIVE_STATUS[j.status] ? 0 : RETRYABLE_STATUS[j.status] ? 1 : 2);
      return rank(a) - rank(b);
    });

    return (
      <div className="ko-jobs">
        <div className="ko-jobs-head">
          <span className="ko-jobs-title serif">Renderjobb</span>
          {actErr && <span className="ko-jobs-err mono">{actErr}</span>}
        </div>
        {jobs === null ? (
          <div className="dim">Läser jobb…</div>
        ) : error ? (
          <div className="ko-jobs-err mono">Kunde inte läsa jobb: {error}</div>
        ) : ordered.length === 0 ? (
          <div className="dim">Inga renderjobb.</div>
        ) : (
          <div className="ko-job-list">
            {ordered.map((j) => (
              <JobRow key={j.id} job={j} busy={busyId === j.id}
                onCancel={onCancel} onRetry={onRetry} />
            ))}
          </div>
        )}
      </div>
    );
  }

  /* ---------- the Kö (queue) modal ---------- */
  // Opened from a GPU pill. Live per-host telemetry cards + the render-queue
  // depth + the owner-only job-management list (lb-07y). Closes on backdrop / ×
  // / Escape. Reuses the .lb-backdrop scrim; a dedicated .ko-modal (single
  // column) rather than the lightbox's 2-col grid.
  function KoModal({ onClose }) {
    const telem = useGpuTelemetry();
    const vm100 = gpuView(telem, "vm100");
    const vm153 = gpuView(telem, "vm153");

    useEffect(() => {
      const onKey = (e) => { if (e.key === "Escape") onClose(); };
      window.addEventListener("keydown", onKey);
      return () => window.removeEventListener("keydown", onKey);
    }, [onClose]);

    const totalQueue = (vm100.g && !vm100.stale ? (Number(vm100.g.queue_depth) || 0) : 0)
      + (vm153.g && !vm153.stale ? (Number(vm153.g.queue_depth) || 0) : 0);

    return (
      <div className="lb-backdrop" onClick={onClose}>
        <div className="ko-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Kö och GPU-status">
          <div className="ko-head">
            <div>
              <div className="ko-title serif">Kö &amp; GPU-status</div>
              <div className="ko-sub mono">{telem ? `${totalQueue} jobb i renderkön` : "Läser telemetri…"}</div>
            </div>
            <button type="button" className="lb-close" onClick={onClose} aria-label="Stäng">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M6 6l12 12M18 6L6 18" /></svg>
            </button>
          </div>
          <div className="ko-body">
            {telem ? (
              <div className="tele-grid">
                <GpuCard name="vm100" sub="RTX 3090 · CUDA" g={vm100.g} stale={vm100.stale} util={vm100.util} />
                <GpuCard name="vm153" sub="Intel B70 · Vulkan" g={vm153.g} stale={vm153.stale} util={vm153.util} />
              </div>
            ) : (
              <div className="empty"><div className="dim">Läser in GPU-telemetri…</div></div>
            )}
            {/* lb-07y: owner-only render-queue management (cancel / retry). */}
            <JobQueueList />
          </div>
        </div>
      </div>
    );
  }

  // Expose for the Babel-in-browser bibliotek.jsx (a separate <script> scope).
  Object.assign(window, { useGpuTelemetry: useGpuTelemetry, LBGpuPills: GpuPills, LBKoModal: KoModal });
})();
