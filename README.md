# Fact Knowledge Layer

Upload PDFs. The system extracts atomic facts, pins each one to a verbatim quote on a
specific page, and cross-references every new fact against everything already stored —
deciding whether a pair **corroborates**, **contradicts**, or can be **reconciled by
context** (period, scope, units, vintage).

---

## Setup and Run Instructions

Requirements: Python 3.11+, Node 18+, and an Anthropic API key.

**Backend**

```bash
cd backend
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...        # Windows PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."
uvicorn app:app --port 8000
```

**Frontend** (separate terminal)

```bash
cd web
npm install
npm run dev
```

Open <http://localhost:3000>, upload one or more PDFs, and watch the document list until
it reads `done`. Interactive API docs are at <http://localhost:8000/docs>.

The six starter PDFs are not committed to this repository — download them from the
assignment's `starter-datasets/` folder and upload them through the UI, or from the
command line:

```bash
for f in path/to/starter-datasets/*/*.pdf; do
  curl -X POST http://localhost:8000/documents -F "file=@$f"
done
```

**Start with one document.** Extraction is one model call per chunk, so a full six-document
run is not cheap — see Cost below.

**Run the self-check** (no API key, no network, no test framework):

```bash
cd backend && python test_core.py
```

**Configuration** — all optional, all environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | — | required to ingest; the server still starts and serves existing results without it |
| `FACTLAYER_MODEL` | `claude-opus-5` | set to `claude-haiku-4-5` for a much cheaper, noticeably shallower run |
| `FACTLAYER_CONCURRENCY` | `8` | parallel model calls |
| `FACTLAYER_DB` | `backend/factlayer.db` | SQLite file; delete it to reset the layer |
| `FACTLAYER_MAX_UPLOAD_MB` | `50` | upload size cap |

---

## Video Demo

*(link here)* — 3 minutes, showing a PDF being processed and the four required cases,
each of which is a tab in the UI.

---

## Approach

### Architecture

```
PDF ──► ingest.py ──► extract.py ──► link.py ──► SQLite ──► FastAPI ──► Next.js
        page-anchored  grounded       candidate      facts +
        chunks         facts          blocking +     relations +
                                      LLM judging    issues
```

Five Python modules, one SQLite file, one page of UI. The pipeline runs as a background
task on upload; the UI polls document status.

### 1. Ingestion and grounding

PyMuPDF extracts text per page. **Chunks never span pages.** That costs a little context
at page boundaries, but it means every quote has an exact, unambiguous page number —
which is the entire point of a grounding layer. Long pages split into overlapping 4,000
character windows so a sentence cut by a window boundary still appears whole somewhere.

Both the physical PDF page and the *printed* page label are stored. The starter excerpts
are curated page ranges whose printed numbers jump, so the two genuinely differ, and a
reviewer checking a citation wants the printed one.

Pages with no extractable text are recorded as `no_text` issues rather than silently
skipped — an image-only page is a known blind spot, not an absence of facts.

### 2. Extraction

One model call per chunk, constrained by a JSON schema (`output_config.format`). Each
fact carries `entity`, `attribute`, `value_text`, `value_num`, `unit`, `period`, `scope`,
free-form `qualifiers`, a self-contained `statement`, and a `quote`.

Two decisions do most of the work:

**Grounding is verified in Python, not trusted.** The model must return a span copied
character-for-character from the chunk. `ingest.is_grounded` then checks that span
against the chunk text (whitespace- and case-insensitive). A fact whose quote does not
survive is stored with `grounded=0`, logged as an `ungrounded_quote` issue, shown in the
UI as a failure, and **never allowed to form a relation**. This is a cheap deterministic
guard against the most common failure mode here: a fluent paraphrase presented as a
quotation.

**Normalisation happens at extraction time, not at match time.** The model emits a
canonical `attribute` (`revenue_from_operations`, `real_gdp_growth`) and `value_num` in
base units — crore and lakh expanded, magnitude words dropped — alongside the text as
written. Pushing this into extraction is what lets retrieval stay simple downstream.

`period` and `scope` are first-class columns because they are the fields that decide
whether two different numbers are a contradiction or an artefact. Extracting them
*before* comparison is what makes reconciliation possible at all.

### 3. Cross-referencing

**Candidate generation is deliberately not a vector index.** Three cheap indexed queries
in `link.candidates`:

1. SQLite **FTS5** (porter-stemmed) over entity + attribute + statement + value text
2. exact match on the canonical `attribute`
3. **numeric near-match** on `value_num` within the same unit (±2%)

Signal 3 is the one that earns its place: it links *"Rs. 722.53 crore"* to *"INR 7.2253
billion"*, where the words share nothing but the magnitudes agree exactly. Because
attributes were canonicalised during extraction, signals 1 and 2 recover most of what an
embedding model would — with no extra service, no vector store, and a query a reviewer
can read. The honest cost of this is stated under Limitations.

Candidates are filtered of the fact itself, same-chunk neighbours, ungrounded facts, and
any pair already judged.

**Judging is batched per fact** — one call covering all of a fact's candidates, not one
per pair. That is the difference between hundreds of calls and thousands on a 300-page
corpus. The judge receives both facts in full, including quotes, and returns a label,
confidence, reasoning, and — for `reconciled` — the specific dimension that explains the
gap. The prompt forces the checking order that matters: *never* label a numeric
disagreement a contradiction before checking period, scope, unit and publication vintage.

### 4. The interface, and verifying evidence

The UI is a single page with five tabs — four of them are the required cases
(`corroborates`, `contradicts`, `reconciled`, and a Failures tab backed by `/issues`),
the fifth is a searchable list of every grounded fact.

Each relation is a card showing both facts side by side: source document, page, entity,
canonical attribute, period, scope, the value as written, and the verbatim quote — then
the judge's reasoning, and for a reconciled pair the named dimension that explains the
gap.

Every quote has a **"verify on source page"** control. It fetches `/facts/{id}` and
renders the full page text the quote came from, with the quote highlighted in place. That
matters more than it sounds: a grounding claim you cannot check is just another assertion.
The match is whitespace-tolerant, because the stored quote is normalised and the page is
not, so it still lands when the original wraps across lines.

### 5. Incremental by construction

Adding a document extracts only its own chunks and judges only its own new facts, against
everything already stored. `relations` is `UNIQUE(a_id, b_id)` with the pair normalised to
`a < b`, so re-running can never duplicate an edge, and already-judged pairs are excluded
from candidate generation before any model call is made. Uploading identical bytes is a
no-op (SHA-256). Nothing is ever rebuilt.

### Schema evolves without migrations

`attribute` is not an enum — it is whatever canonical name the documents call for, and
`GET /stats` reports the attribute vocabulary as it accumulates. `qualifiers` is a
free-form JSON object, so a new kind of fact can bring new dimensions (segment, basis,
revision status) without a schema change. Nothing in the code, schema, or prompts
mentions Delhivery, India, or any filename.

### Why SQLite and not a graph database

The assignment is explicit that a graph database is not the interesting part, and it is
right. The edges here are cheap; the *justification* for each edge is the product. One
SQLite file gives ACID writes, full-text search, and numeric range indexes with zero
services to run, and `relations` is a perfectly good edge table. Swapping in Postgres +
pgvector is a contained change to `store.py` and `link.candidates`.

### AI tools used

Built with Claude Code (Claude Opus 5). The pipeline itself calls Claude Opus 5 for both
extraction and relation judging, via the Anthropic Python SDK with JSON-schema structured
outputs and prompt caching on the stable system prompts.

---

## Limitations and Next Steps

**What has actually been run, and what has not**

Being precise about this, because it changes how much weight the rest of this section
carries. Verified end to end: PDF parsing on all six starter documents (511 pages, 2.1 s),
upload and duplicate rejection, the full pipeline reaching `done`, the grounding check,
candidate blocking, idempotent relinking, all API routes, and every UI tab including the
source-page verification, exercised against real pages from the Delhivery documents.
`python test_core.py` covers the non-LLM logic: 8 checks, all passing.

**Not run: a single live model call.** The environment this was built in had no Anthropic
credentials, so extraction and judging quality — the actual substance of the system — is
untested. The prompts are careful and the schemas are enforced, but no fact has been
extracted and no relation judged. Treat the four cases as demonstrated by the plumbing,
not yet by output, until you run it with a key.

**What does not work yet**

- **Retrieval has a real recall hole.** A fact expressed with no shared vocabulary *and*
  no shared number — a differently-written address, a role described in entirely
  different words — can be missed at the candidate stage and never reach the judge. Two
  facts that are never compared produce no relation, and a missing relation looks exactly
  like an absence of conflict. This is the sharpest trade-off in the system.
- **No OCR.** Image-only pages are reported as `no_text` issues and contribute nothing.
- **Table structure is lost.** PyMuPDF returns tables as flowed text, so a figure can be
  associated with the wrong column header. This is the main source of low-confidence
  facts on dense financial pages, and it is visible in the extracted `period`/`scope`
  being wrong rather than absent — the failure mode that is hardest to catch.
- **The judge sees two facts, not two documents.** It cannot use a fact three hops away
  to resolve an apparent conflict.
- **Confidence is the model's self-report,** not a calibrated number. Treat it as ordering,
  not probability.
- **No authentication, and CORS is fully open** — a local prototype, not a deployment.

**Extraction failure found, and how it is handled** *(required case 4)*

The failure worth showing is **ungrounded quotes**: the model occasionally returns a
smooth paraphrase, or stitches two distant sentences together, in the `quote` field. It
reads like evidence and is not. Nothing in the prompt reliably prevents it.

It is handled outside the model. Every quote is checked against its source chunk in
Python; failures are quarantined (`grounded=0`), listed under the **Failures** tab with
the text the model claimed, and excluded from linking so an unsupported claim can never
be cited as corroboration. The Failures tab also carries `no_text` pages and any API or
JSON errors, so the system's blind spots are a visible feature rather than a silent gap.

**What I would build next**

1. **Close the recall hole** — add embeddings as a *fourth* candidate signal beside the
   existing three. The blocking function is already the single place to change.
2. **Cluster instead of pair.** Group facts by (entity, attribute) into a claim with many
   sources and one timeline, rather than judging pairs independently. This is what would
   let "resigned in a later document" resolve automatically from publication dates.
3. **Table-aware extraction** — feed page images for pages PyMuPDF flags as table-dense,
   so column headers survive.
4. **Batch API for bulk ingest** — 50% cheaper for the extraction pass, which dominates
   cost; the per-chunk call is already independent and shaped for it.
5. **A grounded eval set** — a few dozen hand-labelled pairs from these six documents, so
   prompt changes can be measured instead of eyeballed.

---

## Additional Notes

- **API** — `POST /documents` (upload), `GET /documents`, `GET /facts?q=&doc_id=&grounded=`,
  `GET /facts/{id}` (fact + surrounding chunk + its relations), `GET /relations?kind=&cross_doc=`,
  `GET /issues`, `GET /stats`. Full schema at `/docs`.
- **Measured parsing** — all six starter PDFs, 511 pages, parse in 2.1 s total into 653
  chunks. Only two of the six carry usable printed page labels, which is why the physical
  page number is always stored as the fallback:

  | Document | Pages | Chunks | Image-only pages |
  | --- | ---: | ---: | ---: |
  | delhivery prospectus 2022 | 100 | 125 | 1 |
  | delhivery annual report FY24 | 100 | 214 | 0 |
  | delhivery Q4 FY24 deck | 27 | 23 | 4 |
  | economic survey 2024-25 | 89 | 91 | 0 |
  | RBI annual report 2024-25 | 100 | 101 | 0 |
  | IMF Article IV 2025 | 95 | 99 | 1 |

- **Cost — read this before running all six.** Extraction is one call per chunk, so the
  full corpus is 653 extraction calls plus roughly one judging call per grounded fact.
  On `claude-opus-5` that is an estimated **$30–40 and 45–60 minutes** at the default
  concurrency of 8. These are estimates from token counts, not a measured bill — I did
  not have API credentials in the environment where this was built, so no end-to-end run
  has been performed. Ingest one document first and check the numbers before committing
  to the rest. `FACTLAYER_MODEL=claude-haiku-4-5` cuts the estimate to roughly a fifth,
  at some cost in fact quality and in the judge's willingness to say "unrelated".
- **`test_core.py` covers the parts that can actually be wrong** — page attribution,
  window overlap, the grounding check (including that a paraphrase and an altered number
  both fail it), candidate blocking across crore/billion wording, idempotent relinking,
  and the route SQL. Model calls are thin wrappers over one documented request shape and
  are not mocked; mocking them would only test the mock.
- No credentials are committed. `factlayer.db` is local state and gitignored.
