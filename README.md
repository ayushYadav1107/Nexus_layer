# Fact Knowledge Layer

Upload PDFs. The system extracts atomic facts, pins each one to a verbatim quote on a
specific page, and cross-references every new fact against everything already stored —
deciding whether a pair **corroborates**, **contradicts**, or can be **reconciled by
context** (period, scope, units, vintage).

---

## Setup and Run Instructions

Requirements: Python 3.11+, Node 18+, and a model for each of the two roles.

The default configuration runs **extraction locally on Ollama** (free, unlimited) and
**judging on the Gemini free tier** (far fewer calls, and the reasoning that matters).
Either role can be pointed anywhere — see Configuration.

**Models**

```bash
# extraction, local and free
ollama pull llama3.1:8b

# judging: free key from https://aistudio.google.com/apikey
export GEMINI_API_KEY=...                  # PowerShell: $env:GEMINI_API_KEY="..."
```

**Backend**

```bash
cd backend
pip install -r requirements.txt
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

**Start with one document.** Extraction is one model call per chunk, and locally that is
minutes per chunk — see Cost and the local-inference note below.

**Run the self-check** (no API key, no network, no test framework):

```bash
cd backend && python test_core.py
```

**Configuration** — all optional, all environment variables:

Each role is set as `provider:model`, where provider is `ollama`, `gemini` or
`anthropic`. Mix freely — the whole point of the split is that neither role is pinned.

| Variable | Default | Purpose |
| --- | --- | --- |
| `FACTLAYER_EXTRACT` | `ollama:llama3.1:8b` | model for fact extraction (the many calls) |
| `FACTLAYER_JUDGE` | `gemini:gemini-3.8-flash` | model for relation judging (the calls that matter) |
| `GEMINI_API_KEY` | — | needed if either role is `gemini:` |
| `ANTHROPIC_API_KEY` | — | needed if either role is `anthropic:` |
| `FACTLAYER_JUDGE_RPM` | `14` | requests/min ceiling; keeps the Gemini free tier from 429ing |
| `FACTLAYER_EXTRACT_CONCURRENCY` | `2` | parallel extraction calls |
| `FACTLAYER_JUDGE_CONCURRENCY` | `2` | parallel judging calls |
| `FACTLAYER_OLLAMA_CTX` | `8192` | Ollama context window; see the VRAM note below |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint |
| `FACTLAYER_DB` | `backend/factlayer.db` | SQLite file; delete it to reset the layer |
| `FACTLAYER_MAX_UPLOAD_MB` | `50` | upload size cap |

`GET /stats` reports which model each role resolved to, so the UI always shows what
actually produced the results.

Everything runs on free tiers with these defaults. If you have Anthropic credit, the
best single-model setup is `FACTLAYER_EXTRACT=anthropic:claude-sonnet-5` and the same
for `FACTLAYER_JUDGE`.

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

Six Python modules, one SQLite file, one page of UI. The pipeline runs as a background
task on upload; the UI polls document status.

`llm.py` is the only file that imports a model SDK or knows a provider exists; `extract.py`
and `link.py` import one function from it. That is what makes the extraction/judging
model split a config change rather than a rewrite.

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

One model call per chunk, constrained by a JSON schema — Ollama's `format`, Gemini's
`responseSchema`, Anthropic's `output_config.format`. Each fact carries `entity`, `attribute`, `value_text`, `value_num`, `unit`, `period`, `scope`,
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
canonical `attribute` (`revenue_from_operations`, `real_gdp_growth`) alongside the value
as written. Pushing this into extraction is what lets retrieval stay simple downstream.

The *magnitude*, though, is not left to the model. `value_num` is recomputed in Python
from `value_text` — crore and lakh expanded, accounting parentheses read as negative,
percentages kept as their own number — because every model tested got it wrong somewhere
and numeric blocking depends on it. Arithmetic over a string does not belong in a prompt.

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

**Extraction has been run for real, on Ollama.** `llama3.1:8b` against page 5 of the Q4
FY24 deck: 7 facts, **7/7 quotes verified grounded**, schema honoured exactly. What it
gets wrong is the field discipline, not the structure:

| | result |
| --- | --- |
| quotes grounded | 7/7 |
| `period` populated | 5/7 |
| `scope` populated | 3/7 |
| `value_num` correct | 7/7 *after* the deterministic fix below |
| time per chunk | ~117 s |

Before the prompt carried a worked example, `period` and `scope` were **0/7** — the
model filled the schema and ignored the instructions. Few-shot fixed most of it. What
remains is a real ceiling: an 8B model leaves ~30% of periods and most scopes null, and
those are precisely the fields the judge needs to tell a contradiction from a reporting
difference.

It also mis-normalised numbers every run — `Rs. 127 Cr` as 1.27e8 instead of 1.27e9,
`30%` as 0.3 instead of 30. That is arithmetic over a string, so it was moved out of the
prompt entirely into `extract.value_num_from_text`, which recomputes the magnitude from
the value as written and overrides the model. It also refuses digits glued to letters,
because "PAT profitable in Q3" was otherwise parsing as 3.0 — a spurious number is worse
for numeric blocking than no number.

**Not run: judging.** No Gemini key was available, so no relation has been classified.
The request shape, the JSON-schema translation to Gemini's OpenAPI subset, and the
rate-limit gate are all tested offline, but corroborates/contradicts/reconciled quality
is unverified. Treat the four cases as demonstrated by the plumbing, not yet by output.

**On running extraction locally at all.** `ollama ps` reports `32%/68% CPU/GPU` for
llama3.1:8b at 8k context on a 6 GB RTX 4050 — the model does not fit, so a third of the
layers run on CPU and generation is several times slower than it should be. At ~117 s per
chunk, the 148-chunk deck+prospectus pair is 3–5 hours. If that matters more than the
zero cost, put extraction on Gemini too — 653 calls fits inside the free tier's ~1,500
requests/day:

```bash
export FACTLAYER_EXTRACT=gemini:gemini-3.8-flash
export FACTLAYER_EXTRACT_RPM=14
```

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

- **Cost** — nothing, with the default configuration. Extraction runs locally on
  Ollama and judging on the Gemini free tier (~15 requests/min, ~1,500/day), and the
  in-process rate gate keeps the judge under that ceiling rather than discovering it via
  429s. The binding constraint is time, not money: see the local-inference note above.

  For reference, if you point both roles at Anthropic instead, the full six-document
  corpus is roughly $80 on `claude-opus-5`, $32 on `claude-sonnet-5`, $12 on
  `claude-haiku-4-5` — estimates from token counts, not a measured bill.

  You almost certainly should not run all six documents. The four required cases are
  best shown with the **Q4 FY24 earnings deck plus the 2022 prospectus**: two years
  apart, so period-based reconciliation and changed-director contradictions arise
  naturally, and cross-document links are guaranteed.

- **`test_core.py` covers the parts that can actually be wrong** — page attribution,
  window overlap, the grounding check (including that a paraphrase and an altered number
  both fail it), candidate blocking across crore/billion wording, idempotent relinking,
  and the route SQL. Model calls are thin wrappers over one documented request shape and
  are not mocked; mocking them would only test the mock.
- No credentials are committed. `factlayer.db` is local state and gitignored.
