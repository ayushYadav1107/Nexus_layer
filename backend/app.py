"""FastAPI surface + the ingest -> extract -> link pipeline.

The pipeline is incremental by construction: a new document only extracts its own
chunks, and only its own new facts get judged, against everything already stored.
Nothing existing is recomputed, and `relations` is keyed UNIQUE(a,b) so a re-run
cannot duplicate an edge.
"""
import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

import env  # noqa: F401  loads .env before the reads below

import extract
import ingest
import link
import llm
import store

MAX_UPLOAD = int(os.environ.get("FACTLAYER_MAX_UPLOAD_MB", "50")) * 1024 * 1024
_running = {}   # doc_id -> Task, so a run can be stopped from the UI


@asynccontextmanager
async def lifespan(_app):
    store.init()
    yield


app = FastAPI(title="Fact Knowledge Layer", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------- pipeline

async def _drain(tasks, on_result, doc_id, total):
    """Consume tasks as they finish, persisting each one immediately.

    Results are written per unit rather than after an asyncio.gather over the whole
    document. Gathering first meant a 237-chunk run showed zero facts and zero
    issues for its entire duration, so a silent API failure looked exactly like
    healthy progress -- and cancelling left nothing behind at all.
    """
    done = 0
    try:
        for fut in asyncio.as_completed(tasks):
            on_result(await fut)
            done += 1
            store.set_progress(doc_id, done, total)
    finally:
        # as_completed does not cancel the rest when the caller goes away.
        for t in tasks:
            if not t.done():
                t.cancel()
    return done


async def process(doc_id, filename, pdf_bytes):
    try:
        store.set_doc_status(doc_id, "parsing")
        chunks, empty_pages, n_pages = ingest.parse(pdf_bytes)
        for p in empty_pages:
            store.add_issue(doc_id, p, "no_text",
                            "No extractable text on this page (scanned image, or "
                            "figure-only). Needs OCR; nothing was extracted from it.")
        if not chunks:
            store.set_doc_status(doc_id, "failed", error="No extractable text in document")
            return

        chunk_ids = store.add_chunks(doc_id, chunks)
        store.set_doc_status(doc_id, "extracting", n_pages=n_pages)
        store.set_progress(doc_id, 0, len(chunks))

        new_fact_ids = []

        def keep(result):
            chunk_id, chunk, facts, issues = result
            for iss in issues:
                store.add_issue(doc_id, iss.get("page", chunk["page"]), iss["kind"],
                                iss["detail"], iss.get("payload"))
            for f in facts:
                fid = store.add_fact(doc_id, chunk_id, f)
                if f["grounded"]:
                    new_fact_ids.append(fid)

        async def one_chunk(chunk_id, chunk):
            facts, issues = await extract.extract_chunk(filename, chunk)
            return chunk_id, chunk, facts, issues

        await _drain([asyncio.create_task(one_chunk(cid, c))
                      for cid, c in zip(chunk_ids, chunks)],
                     keep, doc_id, len(chunks))

        store.set_doc_status(doc_id, "linking")
        store.set_progress(doc_id, 0, len(new_fact_ids))

        def record(result):
            verdicts, issues = result
            for iss in issues:
                store.add_issue(doc_id, iss.get("page"), iss["kind"], iss["detail"])
            for v in verdicts:
                store.add_relation(v["a_id"], v["b_id"], v["kind"], v["confidence"],
                                   v["reasoning"], v["resolution"], v["cross_doc"])

        # Judged in waves rather than all at once: facts stored by one wave are
        # candidates for the next, so a document's own internal contradictions get
        # found without a second pass. Concurrency and rate limiting live in llm.py,
        # which is the only place that knows what each role is talking to.
        judged = 0
        for start in range(0, len(new_fact_ids), 32):
            wave = new_fact_ids[start:start + 32]
            await _drain([asyncio.create_task(link.judge(f)) for f in wave],
                         record, doc_id, len(new_fact_ids))
            judged += len(wave)
            store.set_progress(doc_id, judged, len(new_fact_ids))

        store.set_doc_status(doc_id, "done")
        store.set_progress(doc_id, 0, 0)
    except asyncio.CancelledError:
        # Stopped from the UI. Whatever was already extracted stays -- it is real,
        # grounded and useful; only the unfinished remainder is dropped.
        store.set_doc_status(doc_id, "cancelled", error="stopped from the UI")
        store.set_progress(doc_id, 0, 0)
        raise
    except Exception as e:  # never leave a document stuck mid-status
        store.set_doc_status(doc_id, "failed", error=f"{type(e).__name__}: {e}")
        store.add_issue(doc_id, None, "pipeline_error", f"{type(e).__name__}: {e}")
        store.set_progress(doc_id, 0, 0)


# ---------------------------------------------------------------- routes

@app.post("/documents")
async def upload(file: UploadFile):
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"file exceeds {MAX_UPLOAD // 1024 // 1024} MB")
    if not data.startswith(b"%PDF"):
        raise HTTPException(415, "not a PDF")

    doc_id, is_new = store.add_document(file.filename, hashlib.sha256(data).hexdigest())
    if not is_new:
        if doc_id in _running:
            return {"id": doc_id, "status": "duplicate",
                    "detail": "this document is already being processed"}
        with store.db() as con:
            prior = con.execute("SELECT status FROM documents WHERE id=?",
                                (doc_id,)).fetchone()["status"]
        if prior == "done":
            return {"id": doc_id, "status": "duplicate",
                    "detail": "identical file already ingested"}
        # Uploads are deduplicated by content hash, so re-uploading is the natural
        # way to retry a run that failed or was stopped. Clear what it left behind.
        store.reset_document(doc_id)
    # Keyed by document, both to hold a reference (asyncio keeps only a weak one,
    # and a collected task would abandon the run) and so /cancel can find it.
    task = asyncio.create_task(process(doc_id, file.filename, data))
    _running[doc_id] = task
    task.add_done_callback(lambda _t, d=doc_id: _running.pop(d, None))
    return {"id": doc_id, "status": "pending"}


@app.post("/documents/{doc_id}/cancel")
async def cancel(doc_id: int):
    """Stop an in-flight run. Facts already extracted are kept."""
    task = _running.get(doc_id)
    if task is None or task.done():
        raise HTTPException(409, "document is not currently being processed")
    task.cancel()
    return {"id": doc_id, "status": "cancelling"}


@app.get("/documents")
def documents():
    with store.db() as con:
        rows = con.execute("""
            SELECT d.*,
              (SELECT COUNT(*) FROM facts  WHERE doc_id=d.id) AS n_facts,
              (SELECT COUNT(*) FROM facts  WHERE doc_id=d.id AND grounded=0) AS n_ungrounded,
              (SELECT COUNT(*) FROM issues WHERE doc_id=d.id) AS n_issues
            FROM documents d ORDER BY d.id DESC""").fetchall()
    return [dict(r) for r in rows]


def _fact_row(r):
    d = dict(r)
    d["qualifiers"] = json.loads(d.pop("qualifiers") or "{}")
    return d


@app.get("/facts")
def facts(q: str = "", doc_id: int | None = None, grounded: int | None = None, limit: int = 200):
    sql = ["SELECT f.*, d.filename FROM facts f JOIN documents d ON d.id=f.doc_id"]
    args, where = [], []
    if q:
        sql.append("JOIN facts_fts ON facts_fts.rowid = f.id")
        where.append("facts_fts MATCH ?")
        args.append(" OR ".join(f'"{w}"' for w in q.split() if w.isalnum()) or f'"{q}"')
    if doc_id:
        where.append("f.doc_id = ?")
        args.append(doc_id)
    if grounded is not None:
        where.append("f.grounded = ?")
        args.append(grounded)
    if where:
        sql.append("WHERE " + " AND ".join(where))
    sql.append("ORDER BY f.id DESC LIMIT ?")
    args.append(min(limit, 1000))
    with store.db() as con:
        try:
            rows = con.execute(" ".join(sql), args).fetchall()
        except Exception as e:
            raise HTTPException(400, f"bad query: {e}")
    return [_fact_row(r) for r in rows]


@app.get("/facts/{fact_id}")
def fact_detail(fact_id: int):
    with store.db() as con:
        row = con.execute(
            "SELECT f.*, d.filename FROM facts f JOIN documents d ON d.id=f.doc_id WHERE f.id=?",
            (fact_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such fact")
        chunk = con.execute("SELECT text FROM chunks WHERE id=?", (row["chunk_id"],)).fetchone()
        # Explicit aliases: `r.*, f.*` would collide on id/confidence/created_at and
        # sqlite3.Row would silently keep whichever came last.
        rels = con.execute("""
            SELECT r.id AS relation_id, r.kind, r.confidence, r.reasoning, r.resolution,
                   r.cross_doc,
                   f.id AS other_id, f.entity AS other_entity, f.attribute AS other_attribute,
                   f.value_text AS other_value, f.period AS other_period,
                   f.scope AS other_scope, f.statement AS other_statement,
                   f.quote AS other_quote, f.page AS other_page,
                   f.page_label AS other_page_label, d.filename AS other_doc
            FROM relations r
            JOIN facts f ON f.id = CASE WHEN r.a_id=? THEN r.b_id ELSE r.a_id END
            JOIN documents d ON d.id = f.doc_id
            WHERE (r.a_id=? OR r.b_id=?) AND r.kind != 'unrelated'
            ORDER BY r.confidence DESC""",
            (fact_id, fact_id, fact_id)).fetchall()
    return {"fact": _fact_row(row), "evidence_context": chunk["text"] if chunk else None,
            "relations": [dict(r) for r in rels]}


@app.get("/relations")
def relations(kind: str | None = None, cross_doc: int | None = None,
              min_confidence: float = 0.0, limit: int = 200):
    """The main cross-reference view. `kind` filters to one of the demo cases."""
    where = ["r.kind != 'unrelated'", "COALESCE(r.confidence,1) >= ?"]
    args = [min_confidence]
    if kind:
        where[0] = "r.kind = ?"
        args.insert(0, kind)
    if cross_doc is not None:
        where.append("r.cross_doc = ?")
        args.append(cross_doc)
    args.append(min(limit, 1000))
    with store.db() as con:
        rows = con.execute(f"""
            SELECT r.id, r.kind, r.confidence, r.reasoning, r.resolution, r.cross_doc,
                   a.id AS a_id, a.statement AS a_statement, a.quote AS a_quote,
                   a.page AS a_page, a.page_label AS a_page_label, a.period AS a_period,
                   a.scope AS a_scope, a.value_text AS a_value, a.entity AS a_entity,
                   a.attribute AS a_attribute, da.filename AS a_doc,
                   b.id AS b_id, b.statement AS b_statement, b.quote AS b_quote,
                   b.page AS b_page, b.page_label AS b_page_label, b.period AS b_period,
                   b.scope AS b_scope, b.value_text AS b_value, b.entity AS b_entity,
                   b.attribute AS b_attribute, db.filename AS b_doc
            FROM relations r
            JOIN facts a ON a.id=r.a_id JOIN documents da ON da.id=a.doc_id
            JOIN facts b ON b.id=r.b_id JOIN documents db ON db.id=b.doc_id
            WHERE {' AND '.join(where)}
            ORDER BY r.cross_doc DESC, r.confidence DESC LIMIT ?""", args).fetchall()
    return [dict(r) for r in rows]


@app.get("/issues")
def issues(doc_id: int | None = None, limit: int = 200):
    """Case 4: everything the pipeline knows it failed at."""
    sql = "SELECT i.*, d.filename FROM issues i LEFT JOIN documents d ON d.id=i.doc_id"
    args = []
    if doc_id:
        sql += " WHERE i.doc_id=?"
        args.append(doc_id)
    sql += " ORDER BY i.id DESC LIMIT ?"
    args.append(min(limit, 1000))
    with store.db() as con:
        rows = con.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"]) if d["payload"] else None
        out.append(d)
    return out


@app.get("/stats")
def stats():
    """Powers the UI header and shows at a glance that all four cases are present."""
    with store.db() as con:
        one = lambda sql, *a: con.execute(sql, a).fetchone()[0]
        kinds = {r["kind"]: r["n"] for r in con.execute(
            "SELECT kind, COUNT(*) n FROM relations GROUP BY kind")}
        # The "dynamic schema" view: attributes are not declared anywhere, they
        # accumulate as documents introduce new kinds of fact.
        attrs = [dict(r) for r in con.execute(
            "SELECT attribute, COUNT(*) n FROM facts GROUP BY attribute ORDER BY n DESC LIMIT 40")]
        return {
            "documents": one("SELECT COUNT(*) FROM documents"),
            "facts": one("SELECT COUNT(*) FROM facts"),
            "grounded": one("SELECT COUNT(*) FROM facts WHERE grounded=1"),
            "relations": kinds,
            "cross_doc_relations": one(
                "SELECT COUNT(*) FROM relations WHERE cross_doc=1 AND kind!='unrelated'"),
            "issues": one("SELECT COUNT(*) FROM issues"),
            "models": llm.describe(),
            "attributes": attrs,
        }
