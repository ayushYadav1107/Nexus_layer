"""Self-check for everything that is not an LLM call.

The model calls are thin wrappers over a documented request shape; the logic that
can actually be wrong is here: page attribution, the grounding check, candidate
blocking, and idempotent relinking. Run with `python test_core.py`. No API key,
no network, no test framework.
"""
import os
import tempfile

import pymupdf

import ingest

DB_FD, DB_FILE = tempfile.mkstemp(suffix=".db")
os.close(DB_FD)
os.environ["FACTLAYER_DB"] = DB_FILE

import store  # noqa: E402  (must see FACTLAYER_DB)
import link   # noqa: E402

store.DB_PATH = DB_FILE
# added some functions

def make_pdf(pages):
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(40, 40, 560, 780), text, fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


def test_ingest_keeps_page_attribution():
    pdf = make_pdf([
        "Revenue from operations for FY2024 was Rs. 7,225.29 million on a consolidated basis.",
        "",  # a page with nothing extractable
        "The Board appointed Sandeep Kumar Barasia as Executive Director with effect "
        "from 1 April 2021.",
    ])
    chunks, empty, n_pages = ingest.parse(pdf)
    assert n_pages == 3, n_pages
    assert empty == [2], empty                      # blank page reported, not silently dropped
    assert {c["page"] for c in chunks} == {1, 3}, chunks
    assert "7,225.29" in next(c["text"] for c in chunks if c["page"] == 1)


def test_long_page_splits_into_overlapping_windows():
    # A dense table page can exceed the window; splitting must not lose text or
    # cut a sentence out of every copy.
    para = "\n".join(f"Row {i}: gross merchandise value grew steadily." for i in range(400))
    ws = ingest._windows(para)
    assert len(ws) > 1, "a long page should split into windows"
    assert all(len(w) <= ingest.WINDOW for w in ws)
    assert "Row 0:" in ws[0] and f"Row {399}:" in ws[-1], "no text dropped at either end"
    assert ws[0][-200:] in ws[1] or ws[1][:200] in ws[0], "windows must overlap"
    assert len(ingest._windows("short page")) == 1


def test_grounding_check():
    src = "Revenue from  operations for FY2024 was Rs. 7,225.29 million (consolidated)."
    assert ingest.is_grounded("Revenue from operations for FY2024 was Rs. 7,225.29 million", src)
    assert ingest.is_grounded("REVENUE FROM OPERATIONS FOR FY2024", src), "case-insensitive"
    assert ingest.is_grounded("Revenue from\noperations for FY2024", src), "whitespace-insensitive"
    # the failure this whole check exists to catch: a paraphrase sold as a quotation
    assert not ingest.is_grounded("FY2024 revenue was about 7.2 billion rupees", src)
    # and a rewritten number
    assert not ingest.is_grounded("was Rs. 7,225.30 million", src)
    assert not ingest.is_grounded("xy", src), "too short to be evidence"

    # Slide decks and tables give short but perfectly good evidence. A 15-char
    # floor rejected 73% of valid facts on the real Delhivery deck, so the bar is
    # unambiguity: a short quote must occur exactly once in the chunk.
    slide = ("FY24 performance YoY: 29.8% Rs 8,142 Cr revenue from services "
             "YoY: 12.7%(2) 740 mn express parcel shipments 7,054 7,224 8,142")
    assert ingest.is_grounded("YoY: 29.8%", slide), "unique short quote is evidence"
    assert ingest.is_grounded("7,054", slide)
    assert ingest.is_grounded("YoY: 12.7%(2)", slide)
    # 8,142 appears twice here, so on its own it does not pin anything down.
    assert not ingest.is_grounded("8,142", slide), "ambiguous short quote is not"
    # ...but a long quote may legitimately repeat.
    twice = "Revenue from operations grew. Revenue from operations grew."
    assert ingest.is_grounded("Revenue from operations grew.", twice)


def test_value_num_is_recomputed_not_trusted():
    from extract import value_num_from_text as v
    # The two errors llama3.1:8b actually made on the Delhivery deck.
    assert v("Rs. 127 Cr") == 127 * 10**7, "crore is 10^7, not 10^6"
    assert v("30%") == 30.0 and v("30%+") == 30.0, "a percentage is its own number"
    # Indian and international magnitudes on the same page.
    assert v("Rs. 7,225.29 million") == 7225.29e6
    assert v("INR 7.2253 billion") == 7.2253e9
    assert v("2.5 lakh") == 250000.0
    # Accounting parentheses are a negative.
    assert v("Rs. (452 Cr)") == -452 * 10**7
    assert v("-1,008") == -1008.0
    # Plain numbers and units that are not magnitudes.
    assert v("57,000") == 57000.0
    assert v("31 days") == 31.0
    # Digits glued to letters are period tokens, not values. Before this rule
    # "doubled over FY23," parsed as 23.0 and "PAT profitable in Q3" as 3.0,
    # which is worse than no number at all -- it poisons numeric blocking.
    assert v("doubled over FY23, strong Q4") is None
    assert v("PAT profitable in Q3 FY24") is None
    assert v("H1 growth") is None
    # ...but a real value later in the string still wins over a leading period.
    assert v("FY2024 revenue of Rs 500 Cr") == 500 * 10**7
    # Semantic facts have no number, and must not invent one.
    assert v("Sandeep Kumar Barasia, Executive Director") is None
    assert v("") is None and v(None) is None


def test_gemini_schema_translation():
    # generateContent takes an OpenAPI-flavoured subset: no additionalProperties,
    # and nullability as a flag rather than a ["string","null"] type union. This
    # runs against the real schemas because it is the one Gemini-side behaviour
    # that can be checked without a key.
    import json as _json

    import extract as _extract
    import link as _link
    from llm import _gemini_schema

    def walk(node):
        if isinstance(node, dict):
            assert not isinstance(node.get("type"), list), f"type union survived: {node}"
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for schema in (_extract.SCHEMA, _link.SCHEMA):
        out = _gemini_schema(schema)
        assert "additionalProperties" not in _json.dumps(out)
        walk(out)

    item = _gemini_schema(_extract.SCHEMA)["properties"]["facts"]["items"]
    assert item["properties"]["value_num"] == {"type": "number", "nullable": True}
    assert item["properties"]["entity"] == {"type": "string"}, "non-null stays plain"
    assert item["required"], "required list must survive"
    # enum lists are data, not type unions, and must not be mangled
    verdict = _gemini_schema(_link.SCHEMA)["properties"]["verdicts"]["items"]
    assert verdict["properties"]["relationship"]["enum"][0] == "corroborates"


def seed():
    store.init()
    doc_a, _ = store.add_document("prospectus.pdf", "aaa")
    doc_b, _ = store.add_document("annual-report.pdf", "bbb")
    ca = store.add_chunks(doc_a, [{"page": 1, "ordinal": 0, "text": "x"}])[0]
    cb = store.add_chunks(doc_b, [{"page": 9, "ordinal": 0, "text": "y"}])[0]

    def f(doc, chunk, **kw):
        base = dict(entity="Delhivery Limited", attribute="revenue_from_operations",
                    value_text="", value_num=None, unit=None, period=None, scope=None,
                    qualifiers={}, statement="", quote="q" * 20, page=1, grounded=1,
                    confidence=0.9)
        base.update(kw)
        return store.add_fact(doc, chunk, base)

    ids = {}
    ids["crore"] = f(doc_a, ca, value_text="Rs. 722.53 crore", value_num=7225300000.0,
                     unit="INR", period="FY2022", scope="consolidated",
                     statement="Delhivery Limited revenue from operations FY2022 "
                               "was Rs. 722.53 crore consolidated")
    # same amount, different words and magnitude -> must be found via value_num, not text
    ids["billion"] = f(doc_b, cb, entity="Delhivery Ltd", attribute="topline",
                       value_text="INR 7.2253 billion", value_num=7225300000.0,
                       unit="INR", period="FY2022", scope="consolidated",
                       statement="Delhivery Ltd topline of INR 7.2253 billion in FY2022")
    ids["headcount"] = f(doc_b, cb, attribute="headcount", value_text="57,000",
                         value_num=57000.0, unit="count", period="FY2024",
                         statement="Delhivery Limited employed 57,000 people in FY2024")
    ids["ungrounded"] = f(doc_b, cb, attribute="net_profit", value_text="Rs 100 crore",
                          value_num=1e9, unit="INR", grounded=0,
                          statement="Delhivery Limited net profit was Rs 100 crore")
    ids["same_chunk"] = f(doc_a, ca, value_text="Rs. 722.53 crore", value_num=7225300000.0,
                          unit="INR", statement="duplicate on the same page")
    return ids


def test_blocking_finds_numeric_twin_across_wording():
    ids = seed()
    got = {c["id"] for c in link.candidates(ids["crore"])}
    assert ids["billion"] in got, "numeric near-match should bridge crore vs billion wording"
    assert ids["ungrounded"] not in got, "ungrounded facts must never become evidence"
    assert ids["same_chunk"] not in got, "same-chunk facts are not cross-references"
    assert ids["crore"] not in got


def test_fts_query_is_safe_and_useful():
    q = link._fts_query({"entity": 'Delhivery "Limited"', "attribute": "revenue_from_operations",
                         "statement": "The revenue was 7,225.29 (consolidated)!"})
    assert '"' * 2 not in q.replace('" OR "', "|"), "no unbalanced quoting"
    assert '"revenue"' in q and '"delhivery"' in q
    assert '"the"' not in q, "stopwords dropped"
    ids = seed()
    link.candidates(ids["headcount"])  # must not raise on a real FTS MATCH


def test_relinking_is_idempotent():
    ids = seed()
    for _ in range(3):
        store.add_relation(ids["billion"], ids["crore"], "corroborates", 0.9, "same amount",
                           None, True)
    with store.db() as con:
        n = con.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        row = con.execute("SELECT a_id, b_id FROM relations").fetchone()
    assert n == 1, f"re-running the linker duplicated edges ({n})"
    assert row["a_id"] < row["b_id"], "pair must be stored in a canonical order"
    assert ids["crore"] not in {c["id"] for c in link.candidates(ids["billion"])}, \
        "an already-judged pair must not be re-sent to the model"


def test_delete_cascades_and_fts_stays_in_sync():
    ids = seed()
    with store.db() as con:
        con.execute("DELETE FROM facts WHERE id=?", (ids["headcount"],))
        hits = con.execute(
            "SELECT COUNT(*) FROM facts_fts WHERE facts_fts MATCH ?", ('"headcount"',)).fetchone()[0]
    assert hits == 0, "FTS index still holds a deleted fact"


def test_env_file_loads_but_never_overrides_the_shell():
    import tempfile as _tf
    from pathlib import Path

    import env
    d = Path(_tf.mkdtemp())
    (d / ".env").write_text(
        "# a comment\n"
        "\n"
        "FACTLAYER_TEST_PLAIN=hello\n"
        "export FACTLAYER_TEST_EXPORTED=world\n"
        'FACTLAYER_TEST_QUOTED="spaced value"\n'
        "FACTLAYER_TEST_HASH=abc#def\n"          # '#' inside a value is not a comment
        "FACTLAYER_TEST_ALREADY=from-file\n"
        "not-a-pair\n",
        encoding="utf-8")
    os.environ["FACTLAYER_TEST_ALREADY"] = "from-shell"
    try:
        applied = env.load(d / ".env")
        assert os.environ["FACTLAYER_TEST_PLAIN"] == "hello"
        assert os.environ["FACTLAYER_TEST_EXPORTED"] == "world", "export prefix stripped"
        assert os.environ["FACTLAYER_TEST_QUOTED"] == "spaced value", "quotes stripped once"
        assert os.environ["FACTLAYER_TEST_HASH"] == "abc#def", "no inline-comment stripping"
        # The rule that keeps the test suite and CI safe from a stray .env:
        assert os.environ["FACTLAYER_TEST_ALREADY"] == "from-shell"
        assert "FACTLAYER_TEST_ALREADY" not in applied
        assert env.load(d / "nope.env") == [], "a missing file is not an error"
    finally:
        for k in [k for k in os.environ if k.startswith("FACTLAYER_TEST_")]:
            del os.environ[k]


def test_drain_reports_progress_as_work_lands():
    # The bug this replaces: results were gathered for the whole document before
    # anything was written, so a 237-chunk run showed 0 facts and 0 issues for its
    # entire duration and a silent API failure looked exactly like healthy progress.
    import asyncio

    import app
    store.init()
    doc_id, _ = store.add_document("x.pdf", "hash-drain")
    seen = []

    async def unit(n):
        await asyncio.sleep(0.01 * (5 - n))       # finish out of creation order
        return n

    async def go():
        tasks = [asyncio.create_task(unit(i)) for i in range(5)]
        return await app._drain(tasks, seen.append, doc_id, 5)

    assert asyncio.run(go()) == 5
    assert sorted(seen) == [0, 1, 2, 3, 4], "every unit must reach the writer"
    with store.db() as con:
        r = con.execute("SELECT done, total FROM documents WHERE id=?", (doc_id,)).fetchone()
    assert (r["done"], r["total"]) == (5, 5), "progress must be persisted, not just counted"


def test_drain_cancels_leftover_work_when_stopped():
    # Stopping a run from the UI has to stop the outstanding calls too;
    # asyncio.as_completed does not cancel them on its own.
    import asyncio

    import app
    store.init()
    doc_id, _ = store.add_document("y.pdf", "hash-cancel")
    captured = []

    async def go():
        async def quick():
            return "quick"

        async def forever():
            await asyncio.Event().wait()

        tasks = [asyncio.create_task(quick()), asyncio.create_task(forever())]
        captured.append(tasks[1])
        drain = asyncio.create_task(app._drain(tasks, lambda _v: None, doc_id, 2))
        await asyncio.sleep(0.05)
        drain.cancel()
        try:
            await drain
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert captured[0].cancelled(), "outstanding work must be cancelled, not left running"


def test_startup_reconciles_runs_orphaned_by_a_restart():
    # A run lives only inside the process that started it. After a crash or a
    # restart the row still says "extracting", so the UI shows a progress bar
    # forever and Stop answers 409 because there is no task to cancel.
    seed()
    with store.db() as con:
        con.execute("UPDATE documents SET status='extracting', total=214, done=7 WHERE id=1")
        con.execute("UPDATE documents SET status='done' WHERE id=2")

    assert store.mark_orphans() == 1, "only the live document is reconciled"

    with store.db() as con:
        a = con.execute("SELECT status, error, done, total FROM documents WHERE id=1").fetchone()
        b = con.execute("SELECT status FROM documents WHERE id=2").fetchone()
    assert a["status"] == "interrupted"
    assert (a["done"], a["total"]) == (0, 0), "stale progress must not keep a bar moving"
    assert "re-upload" in a["error"], "the row should say how to recover"
    assert b["status"] == "done", "finished documents are left alone"
    assert store.mark_orphans() == 0, "running it again is a no-op"


def test_cancelled_document_can_be_re_ingested():
    # Uploads are deduplicated by content hash, so without reset_document a run
    # that was stopped or failed could never be retried.
    seed()
    doc_id = 1
    store.set_doc_status(doc_id, "cancelled", error="stopped from the UI")
    store.reset_document(doc_id)
    with store.db() as con:
        d = con.execute("SELECT status, error, done, total FROM documents WHERE id=?",
                        (doc_id,)).fetchone()
        n = lambda t: con.execute(
            f"SELECT COUNT(*) c FROM {t} WHERE doc_id=?", (doc_id,)).fetchone()["c"]
        chunks, facts, issues = n("chunks"), n("facts"), n("issues")
    assert d["status"] == "pending" and d["error"] is None
    assert (d["done"], d["total"]) == (0, 0)
    assert chunks == 0, "chunks cleared so a retry does not duplicate them"
    assert facts == 0, "facts cascade from chunks"
    assert issues == 0


def test_api_routes_return_what_the_ui_needs():
    import app  # imported late: it pulls in the LLM modules
    ids = seed()
    store.add_relation(ids["crore"], ids["billion"], "corroborates", 0.95,
                       "722.53 crore and 7.2253 billion rupees are the same amount", None, True)
    store.add_relation(ids["crore"], ids["headcount"], "reconciled", 0.6,
                       "different measures", "different attribute entirely", False)
    store.add_issue(1, 4, "no_text", "scanned page")

    corr = app.relations(kind="corroborates")
    assert len(corr) == 1 and corr[0]["cross_doc"] == 1
    assert corr[0]["a_doc"] != corr[0]["b_doc"], "cross-document pair must name both files"
    assert corr[0]["a_quote"] and corr[0]["b_quote"], "UI needs evidence on both sides"

    assert len(app.relations(kind="reconciled")) == 1
    assert app.relations(kind="contradicts") == []
    assert len(app.relations()) == 2, "no filter returns every non-unrelated edge"
    assert len(app.relations(cross_doc=1)) == 1
    assert len(app.relations(min_confidence=0.9)) == 1

    detail = app.fact_detail(ids["crore"])
    assert detail["fact"]["id"] == ids["crore"]
    assert isinstance(detail["fact"]["qualifiers"], dict)
    assert {r["other_id"] for r in detail["relations"]} == {ids["billion"], ids["headcount"]}
    assert detail["relations"][0]["relation_id"] != detail["relations"][0]["other_id"], \
        "relation id must not be shadowed by the fact id"

    assert len(app.facts(q="headcount")) == 1
    assert len(app.facts(grounded=0)) == 1, "ungrounded facts stay visible as failures"
    assert len(app.facts(doc_id=1)) == 2

    s = app.stats()
    assert s["relations"]["corroborates"] == 1 and s["grounded"] == 4
    assert any(a["attribute"] == "revenue_from_operations" for a in s["attributes"])
    assert app.issues()[0]["kind"] == "no_text"


def reset():
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(DB_FILE + suffix)
        except OSError:
            pass


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        reset()
        t()
        print(f"  ok  {t.__name__}")
    reset()
    print(f"\n{len(tests)} passed")
