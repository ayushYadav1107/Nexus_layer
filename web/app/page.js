"use client";

import { useCallback, useEffect, useState } from "react";

const api = (p) => fetch(`/api${p}`).then((r) => r.json());
const LIVE = ["pending", "parsing", "extracting", "linking"];
const nf = new Intl.NumberFormat();

const VIEWS = [
  { key: "corroborates", label: "Corroborated", hint: "Same claim, stated differently" },
  { key: "contradicts", label: "Contradictions", hint: "Incompatible under the same period and scope" },
  { key: "reconciled", label: "Reconciled", hint: "Differ for a stated reason" },
  { key: "issues", label: "Failures", hint: "What the pipeline got wrong or could not read" },
  { key: "facts", label: "All facts", hint: "Every grounded fact, searchable" },
];

/** Page text around a quote with the quote marked, so a reviewer can confirm the
 *  evidence rather than take our word for it. Whitespace-tolerant, because the
 *  stored quote is normalised and the page is not. */
function Highlighted({ context, quote }) {
  if (!context) return null;
  const pattern = quote.trim().replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s+");
  let parts = [context];
  try {
    const re = new RegExp(`(${pattern})`, "i");
    if (re.test(context)) parts = context.split(re);
  } catch {
    /* pathological quote: fall back to plain context */
  }
  return (
    <pre className="context">
      {parts.map((part, i) =>
        i % 2 === 1 ? <mark key={i}>{part}</mark> : <span key={i}>{part}</span>
      )}
    </pre>
  );
}

function Evidence({ factId, quote }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState(null);

  useEffect(() => {
    if (open && !data) api(`/facts/${factId}`).then(setData).catch(() => setData({}));
  }, [open, data, factId]);

  return (
    <>
      <button className="link" onClick={() => setOpen(!open)}>
        {open ? "hide source page" : "verify on source page"}
      </button>
      {open && (
        <div className="evidence">
          {!data && <span className="meta">loading…</span>}
          {data && <Highlighted context={data.evidence_context} quote={quote} />}
          {data?.relations?.length > 0 && (
            <div className="meta" style={{ marginTop: 8 }}>
              Also linked to:{" "}
              {data.relations
                .map((r) => `${r.kind} — ${r.other_doc} p.${r.other_page_label || r.other_page}`)
                .join("; ")}
            </div>
          )}
        </div>
      )}
    </>
  );
}

function Source({ p, s }) {
  const page = s[`${p}_page_label`]
    ? `p.${s[`${p}_page_label`]} (pdf ${s[`${p}_page`]})`
    : `p.${s[`${p}_page`]}`;
  return (
    <div className="side">
      <div className="src">{s[`${p}_doc`]} <span className="dot">·</span> {page}</div>
      <div className="val">{s[`${p}_value`]}</div>
      <div className="meta">
        {s[`${p}_entity`]} <span className="dot">·</span>
        <span className="attr">{s[`${p}_attribute`]}</span>
        {s[`${p}_period`] ? <> <span className="dot">·</span> {s[`${p}_period`]}</> : null}
        {s[`${p}_scope`] ? <> <span className="dot">·</span> {s[`${p}_scope`]}</> : null}
      </div>
      <blockquote>{s[`${p}_quote`]}</blockquote>
      <Evidence factId={s[`${p}_id`]} quote={s[`${p}_quote`]} />
    </div>
  );
}

function Relation({ r, i }) {
  return (
    <div className={`card rel k-${r.kind}`} style={{ animationDelay: `${Math.min(i, 12) * 45}ms` }}>
      <div className="spread">
        <span className="tag">{r.kind}</span>
        <span className="meta">
          {r.cross_doc ? "across documents" : "within one document"}
          {r.confidence != null ? ` · confidence ${r.confidence.toFixed(2)}` : ""}
        </span>
      </div>
      <div className="pair">
        <Source p="a" s={r} />
        <Source p="b" s={r} />
      </div>
      <p className="why"><b>Reasoning</b><br />{r.reasoning}</p>
      {r.resolution && <div className="resolution"><b>Reconciled by:</b> {r.resolution}</div>}
    </div>
  );
}

function Fact({ f }) {
  return (
    <div className="fact">
      <div className="hd">
        <strong>{f.value_text}</strong>
        <span className="meta">{f.entity}</span>
        <span className="attr">{f.attribute}</span>
        {!f.grounded && <span className="tag k-contradicts">quote unverified</span>}
      </div>
      <div className="meta">
        {f.filename} <span className="dot">·</span> p.{f.page_label || f.page}
        {f.period ? <> <span className="dot">·</span> {f.period}</> : null}
        {f.scope ? <> <span className="dot">·</span> {f.scope}</> : null}
      </div>
      <blockquote>{f.quote}</blockquote>
      <Evidence factId={f.id} quote={f.quote} />
    </div>
  );
}

/** The pipeline as a funnel of real counts. Doubles as an explanation of what the
 *  system does, which is most of what the page needed to stop feeling empty. */
function Funnel({ s }) {
  const stages = [
    { n: s.chunks, label: "chunks", note: "page-anchored" },
    { n: s.facts, label: "facts", note: "extracted" },
    { n: s.grounded, label: "grounded", note: "quote verified" },
    { n: s.linked, label: "relations", note: "cross-referenced" },
  ];
  return (
    <div className="funnel">
      {stages.map((st, i) => (
        <div className="stage" key={st.label}>
          <b>{nf.format(st.n ?? 0)}</b>
          <span>{st.label}</span>
          <em>{st.note}</em>
          {i < stages.length - 1 && <i className="arrow" aria-hidden="true" />}
        </div>
      ))}
    </div>
  );
}

/** Status, so both segments carry a text label -- identity is never colour alone. */
function GroundingBar({ grounded, ungrounded }) {
  const total = grounded + ungrounded;
  if (!total) return null;
  const pct = Math.round((grounded / total) * 100);
  return (
    <div className="panel">
      <div className="spread">
        <h3>Evidence check</h3>
        <span className="meta">{pct}% of extracted facts verified</span>
      </div>
      <div className="ratio">
        <i className="ok" style={{ flex: grounded }} />
        <i className="bad" style={{ flex: ungrounded }} />
      </div>
      <div className="legend">
        <span><i className="sw ok" />{nf.format(grounded)} quote found verbatim</span>
        <span><i className="sw bad" />{nf.format(ungrounded)} unverified — quarantined</span>
      </div>
    </div>
  );
}

/** Magnitude across categories, so one hue -- not a categorical palette. */
function AttributeBars({ attributes }) {
  const top = (attributes || []).slice(0, 8);
  if (!top.length) return null;
  const max = Math.max(...top.map((a) => a.n));
  return (
    <div className="panel">
      <div className="spread">
        <h3>Most common fact types</h3>
        <span className="meta">{attributes.length} distinct, none predeclared</span>
      </div>
      <ul className="bars">
        {top.map((a) => (
          <li key={a.attribute}>
            <span className="blabel" title={a.attribute}>{a.attribute}</span>
            <span className="btrack"><i style={{ width: `${(a.n / max) * 100}%` }} /></span>
            <span className="bval">{a.n}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function DocCard({ d, live, selected, onSelect, onStop, stopping }) {
  const pct = d.total > 0 ? Math.round((d.done / d.total) * 100) : 0;
  return (
    <div className={`doccard${selected ? " on" : ""}`} onClick={() => onSelect(d.id)}
         role="button" tabIndex={0}
         onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onSelect(d.id)}>
      <div className="spread">
        <span className="name" title={d.filename}>{d.filename}</span>
        <span className="pill" data-s={d.status} data-live={live ? "1" : "0"}>{d.status}</span>
      </div>
      <div className="dstats">
        <span><b>{d.n_pages ?? "—"}</b> pages</span>
        <span><b>{nf.format(d.n_chunks ?? 0)}</b> chunks</span>
        <span><b>{nf.format(d.n_grounded ?? 0)}</b> facts</span>
        <span><b>{nf.format(d.n_issues ?? 0)}</b> issues</span>
      </div>
      {live && (
        <>
          <div className={`bar${d.total > 0 ? "" : " indet"}`}><i style={{ width: `${pct}%` }} /></div>
          <div className="spread" style={{ marginTop: 8 }}>
            <span className="meta">{d.status} {d.done}/{d.total} ({pct}%)</span>
            <button className="stop" disabled={stopping}
                    onClick={(e) => { e.stopPropagation(); onStop(d.id); }}>
              {stopping ? "stopping…" : "Stop"}
            </button>
          </div>
        </>
      )}
      {d.error && !live && <p className="meta derr">{d.error}</p>}
    </div>
  );
}

export default function Home() {
  const [view, setView] = useState("corroborates");
  const [stats, setStats] = useState(null);
  const [docs, setDocs] = useState([]);
  const [items, setItems] = useState([]);
  const [docId, setDocId] = useState(null);          // null = every document
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [stopping, setStopping] = useState(null);
  const [err, setErr] = useState("");
  const [notice, setNotice] = useState("");
  const [tick, setTick] = useState(0);

  const refresh = useCallback(async () => {
    try {
      const [s, d] = await Promise.all([api("/stats"), api("/documents")]);
      setStats(s);
      setDocs(d);
      setErr("");
    } catch {
      setErr("Backend unreachable — is uvicorn running on :8000?");
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  useEffect(() => {
    const anyLive = docs.some((d) => LIVE.includes(d.status));
    const id = setInterval(() => {
      refresh();
      if (anyLive) setTick((t) => t + 1);
    }, anyLive ? 1500 : 10000);
    return () => clearInterval(id);
  }, [docs, refresh]);

  useEffect(() => {
    const doc = docId ? `&doc_id=${docId}` : "";
    const path =
      view === "issues" ? `/issues?limit=200${doc}`
      : view === "facts" ? `/facts?limit=200&q=${encodeURIComponent(q)}${doc}`
      : `/relations?kind=${view}${doc}`;
    api(path).then(setItems).catch(() => setItems([]));
  }, [view, q, docId, tick, stats?.facts, stats?.issues]);

  async function upload(e) {
    const files = [...e.target.files];
    e.target.value = "";
    if (!files.length) return;
    setBusy(true);
    setErr("");
    setNotice("");
    const notes = [];
    try {
      for (const file of files) {
        const body = new FormData();
        body.append("file", file);
        const res = await fetch("/api/documents", { method: "POST", body });
        const out = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(`${file.name}: ${out.detail || res.status}`);
        // Say what the server did. A duplicate returns 200 and changes nothing,
        // which is indistinguishable from a dead button unless we report it.
        notes.push(out.status === "duplicate"
          ? `${file.name}: ${out.detail}`
          : `${file.name}: processing started`);
      }
      await refresh();
    } catch (e2) {
      setErr(String(e2.message || e2));
    } finally {
      setNotice(notes.join(" · "));
      setBusy(false);
    }
  }

  async function stop(id) {
    setStopping(id);
    try {
      const res = await fetch(`/api/documents/${id}/cancel`, { method: "POST" });
      if (!res.ok) setErr(`Could not stop: ${(await res.json()).detail || res.status}`);
      await refresh();
    } catch (e2) {
      setErr(String(e2.message || e2));
    } finally {
      setStopping(null);
    }
  }

  const rel = stats?.relations || {};
  const counts = {
    corroborates: rel.corroborates || 0,
    contradicts: rel.contradicts || 0,
    reconciled: rel.reconciled || 0,
    issues: stats?.issues || 0,
    facts: stats?.grounded || 0,
  };
  const perDoc = stats?.per_document || [];
  const chunks = perDoc.reduce((n, d) => n + (d.n_chunks || 0), 0);
  const ungrounded = perDoc.reduce((n, d) => n + (d.n_ungrounded || 0), 0);
  const linked = counts.corroborates + counts.contradicts + counts.reconciled;
  const selectedName = docId && perDoc.find((d) => d.id === docId)?.filename;

  return (
    <main className="wrap">
      <header className="hero">
        <h1>Nexus Layer</h1>
        <p className="sub">
          Facts extracted from PDFs, each pinned to a verbatim quote on a specific page,
          then cross-referenced against every fact already in the layer — corroborating,
          contradicting, or reconciled by context.
        </p>
      </header>

      <div className="card">
        <div className="spread">
          <div className="stats">
            <div className="stat"><b>{stats?.documents ?? "—"}</b><span>documents</span></div>
            <div className="stat"><b>{nf.format(stats?.grounded ?? 0)}</b><span>grounded facts</span></div>
            <div className="stat"><b>{nf.format(stats?.cross_doc_relations ?? 0)}</b><span>cross-doc links</span></div>
            <div className="stat"><b>{stats?.attributes?.length ?? "—"}</b><span>fact types</span></div>
          </div>
          <label className="filebtn">
            {busy ? "Uploading…" : "Upload PDFs"}
            <input type="file" accept="application/pdf" multiple onChange={upload} disabled={busy} />
          </label>
        </div>

        {stats?.models && (
          <p className="meta models">
            extraction <span className="attr">{stats.models.extract}</span>
            <span className="dot">·</span>
            judging <span className="attr">{stats.models.judge}</span>
          </p>
        )}
        {err && <p className="err">{err}</p>}
        {notice && <p className="notice">{notice}</p>}

        {stats?.facts > 0 && (
          <Funnel s={{ chunks, facts: stats.facts, grounded: stats.grounded, linked }} />
        )}
      </div>

      {perDoc.length > 0 && (
        <>
          <div className="secthead">
            <h2>Documents</h2>
            <span className="meta">
              {docId ? `filtered to ${selectedName}` : "click one to filter everything below"}
            </span>
            {docId && <button className="link" onClick={() => setDocId(null)}>show all</button>}
          </div>
          <div className="docgrid">
            {perDoc.map((d) => {
              const row = docs.find((x) => x.id === d.id) || {};
              const merged = { ...d, done: row.done, total: row.total, error: row.error };
              return (
                <DocCard key={d.id} d={merged} live={LIVE.includes(d.status)}
                         selected={docId === d.id} stopping={stopping === d.id}
                         onSelect={(id) => setDocId(docId === id ? null : id)}
                         onStop={stop} />
              );
            })}
          </div>
        </>
      )}

      {stats?.facts > 0 && (
        <div className="panels">
          <GroundingBar grounded={stats.grounded} ungrounded={ungrounded} />
          <AttributeBars attributes={stats.attributes} />
        </div>
      )}

      <div className="secthead">
        <h2>Cross-references</h2>
        {docId && <span className="meta">within {selectedName}</span>}
      </div>

      <div className="row tabs">
        {VIEWS.map((v) => (
          <button key={v.key} aria-pressed={view === v.key} title={v.hint}
                  onClick={() => setView(v.key)}>
            {v.label}<span className="count">{counts[v.key]}</span>
          </button>
        ))}
        {view === "facts" && (
          <input type="search" placeholder="Search facts…" value={q}
                 onChange={(e) => setQ(e.target.value)} />
        )}
      </div>

      {items.length === 0 && (
        <div className="empty">
          <p>
            {docs.length === 0
              ? "Upload a PDF to begin."
              : docs.some((d) => LIVE.includes(d.status))
              ? "Still processing — results appear here as they are found."
              : `No ${VIEWS.find((v) => v.key === view)?.label.toLowerCase()} yet${docId ? " for this document" : ""}.`}
          </p>
          {docId && <button className="link" onClick={() => setDocId(null)}>clear the document filter</button>}
        </div>
      )}

      {view === "issues" && items.map((i, n) => (
        <div className="card rel k-issue" key={i.id}
             style={{ animationDelay: `${Math.min(n, 12) * 45}ms` }}>
          <div className="spread">
            <span className="tag">{i.kind}</span>
            <span className="meta">{i.filename}{i.page ? ` · p.${i.page}` : ""}</span>
          </div>
          <p className="why">{i.detail}</p>
          {i.payload?.quote && <blockquote>Model claimed quote: {i.payload.quote}</blockquote>}
        </div>
      ))}

      {view === "facts" && items.length > 0 && (
        <div className="card">{items.map((f) => <Fact key={f.id} f={f} />)}</div>
      )}

      {!["issues", "facts"].includes(view) && items.map((r, n) => (
        <Relation key={r.id} r={r} i={n} />
      ))}
    </main>
  );
}
