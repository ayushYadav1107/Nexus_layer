"use client";

import { useCallback, useEffect, useState } from "react";

const api = (p) => fetch(`/api${p}`).then((r) => r.json());

const LIVE = ["pending", "parsing", "extracting", "linking"];

// The four views the assignment asks to be demonstrated, plus a fact browser.
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

function Doc({ d, onStop, stopping }) {
  const live = LIVE.includes(d.status);
  const pct = d.total > 0 ? Math.round((d.done / d.total) * 100) : 0;
  return (
    <li>
      <div className="spread">
        <span className="name">{d.filename}</span>
        <span className="row" style={{ gap: 8 }}>
          <span className="pill" data-s={d.status} data-live={live ? "1" : "0"}>{d.status}</span>
          {live && (
            <button className="stop" disabled={stopping} onClick={() => onStop(d.id)}>
              {stopping ? "stopping…" : "Stop"}
            </button>
          )}
        </span>
      </div>
      <div className="meta">
        {d.n_pages ? `${d.n_pages} pages` : "reading…"}
        {d.n_facts ? <> <span className="dot">·</span> {d.n_facts} facts</> : null}
        {d.n_issues ? <> <span className="dot">·</span> {d.n_issues} issues</> : null}
        {live && d.total > 0 && (
          <> <span className="dot">·</span> {d.status} {d.done}/{d.total} ({pct}%)</>
        )}
        {d.error ? <> <span className="dot">·</span> {d.error}</> : null}
      </div>
      {live && (
        <div className={`bar${d.total > 0 ? "" : " indet"}`}>
          <i style={{ width: `${pct}%` }} />
        </div>
      )}
    </li>
  );
}

export default function Home() {
  const [view, setView] = useState("corroborates");
  const [stats, setStats] = useState(null);
  const [docs, setDocs] = useState([]);
  const [items, setItems] = useState([]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [stopping, setStopping] = useState(null);
  const [err, setErr] = useState("");
  const [tick, setTick] = useState(0);

  const refresh = useCallback(async () => {
    try {
      const [s, d] = await Promise.all([api("/stats"), api("/documents")]);
      setStats(s);
      setDocs(d);
      setErr("");
      return d;
    } catch {
      setErr("Backend unreachable — is uvicorn running on :8000?");
      return [];
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // Poll fast while work is in flight, slowly when idle.
  useEffect(() => {
    const anyLive = docs.some((d) => LIVE.includes(d.status));
    const id = setInterval(() => {
      refresh();
      if (anyLive) setTick((t) => t + 1);
    }, anyLive ? 1500 : 10000);
    return () => clearInterval(id);
  }, [docs, refresh]);

  useEffect(() => {
    const path =
      view === "issues" ? "/issues"
      : view === "facts" ? `/facts?limit=200&q=${encodeURIComponent(q)}`
      : `/relations?kind=${view}`;
    api(path).then(setItems).catch(() => setItems([]));
  }, [view, q, tick, stats?.facts, stats?.issues]);

  async function upload(e) {
    const files = [...e.target.files];
    e.target.value = "";
    setBusy(true);
    setErr("");
    try {
      for (const file of files) {
        const body = new FormData();
        body.append("file", file);
        const res = await fetch("/api/documents", { method: "POST", body });
        if (!res.ok) throw new Error(`${file.name}: ${res.status} ${await res.text()}`);
      }
      await refresh();
    } catch (e2) {
      setErr(String(e2.message || e2));
    } finally {
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
  const models = stats?.models;

  return (
    <main className="wrap">
      <h1>Fact Knowledge Layer</h1>
      <p className="sub">
        Facts extracted from PDFs, each pinned to a verbatim quote on a specific page, then
        cross-referenced against every fact already in the layer.
      </p>

      <div className="card">
        <div className="spread">
          <div className="stats">
            <div className="stat"><b>{stats?.documents ?? "—"}</b><span>documents</span></div>
            <div className="stat"><b>{stats?.grounded ?? "—"}</b><span>grounded facts</span></div>
            <div className="stat"><b>{stats?.cross_doc_relations ?? "—"}</b><span>cross-doc links</span></div>
            <div className="stat"><b>{stats?.attributes?.length ?? "—"}</b><span>fact types</span></div>
          </div>
          <label className="filebtn">
            {busy ? "Uploading…" : "Upload PDFs"}
            <input type="file" accept="application/pdf" multiple onChange={upload} disabled={busy} />
          </label>
        </div>

        {models && (
          <p className="meta" style={{ marginTop: 12 }}>
            extraction <span className="attr">{models.extract}</span>
            <span className="dot">·</span>
            judging <span className="attr">{models.judge}</span>
          </p>
        )}
        {err && <p className="err">{err}</p>}

        {docs.length > 0 && (
          <ul className="docs">
            {docs.map((d) => (
              <Doc key={d.id} d={d} onStop={stop} stopping={stopping === d.id} />
            ))}
          </ul>
        )}
      </div>

      <div className="row tabs" style={{ marginBottom: 18 }}>
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
        <p className="empty">
          {docs.length === 0
            ? "Upload a PDF to begin."
            : docs.some((d) => LIVE.includes(d.status))
            ? "Still processing — results appear here as they are found."
            : "Nothing found in this view yet."}
        </p>
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
