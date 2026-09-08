"use client";

import { useCallback, useEffect, useState } from "react";

const api = (p) => fetch(`/api${p}`).then((r) => r.json());

// The four views the assignment asks to be demonstrated, in order.
const VIEWS = [
  { key: "corroborates", label: "Corroborated", hint: "Same claim, stated differently" },
  { key: "contradicts", label: "Contradictions", hint: "Incompatible under the same period and scope" },
  { key: "reconciled", label: "Reconciled by context", hint: "Differ for a stated reason" },
  { key: "issues", label: "Failures", hint: "What the pipeline got wrong or could not read" },
  { key: "facts", label: "All facts", hint: "Every grounded fact, searchable" },
];

/** Renders the page text around a quote with the quote itself marked, so a
 *  reviewer can confirm the evidence is real rather than take our word for it.
 *  Whitespace-tolerant, because the stored quote is normalised and the page is not. */
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

/** Lazy-loads /facts/{id}: the full page text the quote came from, plus every
 *  other relation this fact participates in. */
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
              {data.relations.map((r) => `${r.kind} — ${r.other_doc} p.${r.other_page_label || r.other_page}`).join("; ")}
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
      <div className="src">{s[`${p}_doc`]} · {page}</div>
      <div className="val">{s[`${p}_value`]}</div>
      <div className="meta">
        {s[`${p}_entity`]} · <span className="attr">{s[`${p}_attribute`]}</span>
        {s[`${p}_period`] ? ` · ${s[`${p}_period`]}` : ""}
        {s[`${p}_scope`] ? ` · ${s[`${p}_scope`]}` : ""}
      </div>
      <blockquote>{s[`${p}_quote`]}</blockquote>
      <Evidence factId={s[`${p}_id`]} quote={s[`${p}_quote`]} />
    </div>
  );
}

function Relation({ r }) {
  return (
    <div className="card">
      <div className="spread">
        <span className={`tag k-${r.kind}`}>{r.kind}</span>
        <span className="meta status">
          {r.cross_doc ? "across documents" : "within one document"}
          {r.confidence != null ? ` · confidence ${r.confidence.toFixed(2)}` : ""}
        </span>
      </div>
      <div className="pair">
        <Source p="a" s={r} />
        <Source p="b" s={r} />
      </div>
      <p className="why"><b>Reasoning — </b>{r.reasoning}</p>
      {r.resolution && <div className="resolution"><b>Reconciled by:</b> {r.resolution}</div>}
    </div>
  );
}

function Fact({ f }) {
  return (
    <div className="fact">
      <div className="hd">
        <strong>{f.value_text}</strong>
        <span className="attr">{f.entity} · {f.attribute}</span>
        {!f.grounded && <span className="tag k-contradicts">quote unverified</span>}
      </div>
      <div className="meta">
        {f.filename} · p.{f.page_label || f.page}
        {f.period ? ` · ${f.period}` : ""}{f.scope ? ` · ${f.scope}` : ""}
      </div>
      <blockquote>{f.quote}</blockquote>
      <Evidence factId={f.id} quote={f.quote} />
    </div>
  );
}

export default function Home() {
  const [view, setView] = useState("corroborates");
  const [stats, setStats] = useState(null);
  const [docs, setDocs] = useState([]);
  const [items, setItems] = useState([]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

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

  useEffect(() => {
    refresh();
    // Poll only while something is still being processed.
    const t = setInterval(() => {
      if (docs.some((d) => !["done", "failed"].includes(d.status))) refresh();
    }, 3000);
    return () => clearInterval(t);
  }, [refresh, docs]);

  useEffect(() => {
    const path =
      view === "issues" ? "/issues"
      : view === "facts" ? `/facts?limit=200&q=${encodeURIComponent(q)}`
      : `/relations?kind=${view}`;
    api(path).then(setItems).catch(() => setItems([]));
  }, [view, q, stats]);

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

  const rel = stats?.relations || {};
  const counts = {
    corroborates: rel.corroborates || 0,
    contradicts: rel.contradicts || 0,
    reconciled: rel.reconciled || 0,
    issues: stats?.issues || 0,
    facts: stats?.grounded || 0,
  };

  return (
    <main className="wrap">
      <h1>Fact Knowledge Layer</h1>
      <p className="sub">
        Facts extracted from PDFs, each pinned to a verbatim quote, then cross-referenced
        against every fact already in the layer.
      </p>

      <div className="card">
        <div className="row spread">
          <div className="stats">
            <div className="stat"><b>{stats?.documents ?? "—"}</b><span>documents</span></div>
            <div className="stat"><b>{stats?.grounded ?? "—"}</b><span>grounded facts</span></div>
            <div className="stat"><b>{stats?.cross_doc_relations ?? "—"}</b><span>cross-document links</span></div>
            <div className="stat"><b>{stats?.attributes?.length ?? "—"}</b><span>fact types seen</span></div>
          </div>
          <label className="filebtn">
            {busy ? "Uploading…" : "Upload PDFs"}
            <input type="file" accept="application/pdf" multiple onChange={upload} disabled={busy} />
          </label>
        </div>
        {err && <p className="err">{err}</p>}
        {docs.length > 0 && (
          <ul className="docs">
            {docs.map((d) => (
              <li key={d.id}>
                <span>{d.filename}</span>
                <span className="status" data-s={d.status}>
                  {d.status}
                  {d.n_facts ? ` · ${d.n_facts} facts` : ""}
                  {d.n_issues ? ` · ${d.n_issues} issues` : ""}
                  {d.error ? ` · ${d.error}` : ""}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="row" style={{ marginBottom: 14 }}>
        {VIEWS.map((v) => (
          <button
            key={v.key}
            aria-pressed={view === v.key}
            title={v.hint}
            onClick={() => setView(v.key)}
          >
            {v.label} ({counts[v.key]})
          </button>
        ))}
        {view === "facts" && (
          <input
            type="search"
            placeholder="Search facts…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        )}
      </div>

      {items.length === 0 && (
        <p className="empty">
          Nothing here yet. {docs.length === 0 ? "Upload a PDF to begin." : "Still processing, or none found."}
        </p>
      )}

      {view === "issues" && items.map((i) => (
        <div className="card" key={i.id}>
          <div className="spread">
            <span className="tag k-issue">{i.kind}</span>
            <span className="status">{i.filename}{i.page ? ` · p.${i.page}` : ""}</span>
          </div>
          <p className="why">{i.detail}</p>
          {i.payload?.quote && (
            <blockquote>Model claimed quote: {i.payload.quote}</blockquote>
          )}
        </div>
      ))}

      {view === "facts" && items.length > 0 && (
        <div className="card">{items.map((f) => <Fact key={f.id} f={f} />)}</div>
      )}

      {!["issues", "facts"].includes(view) && items.map((r) => <Relation key={r.id} r={r} />)}
    </main>
  );
}
