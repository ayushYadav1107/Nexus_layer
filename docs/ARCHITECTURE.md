# Architecture

Six Python modules, one SQLite file, one page of UI. No services to run beyond the two
dev servers, and no infrastructure to provision.

---

## System

```mermaid
flowchart LR
  subgraph Browser
    UI["Next.js single page<br/>tabs · document filter · evidence viewer"]
  end

  subgraph Backend["FastAPI (uvicorn :8000)"]
    API["app.py<br/>routes + pipeline orchestration"]
    ING["ingest.py<br/>PDF → page-anchored chunks"]
    EXT["extract.py<br/>chunk → grounded facts"]
    LNK["link.py<br/>candidates → judged relations"]
    LLM["llm.py<br/>the only provider-aware file"]
  end

  DB[("SQLite<br/>documents · chunks · facts<br/>relations · issues · FTS5")]

  M1["Extraction model"]
  M2["Judging model"]

  UI -- "/api/* proxied by Next" --> API
  API --> ING --> DB
  API --> EXT --> LLM --> M1
  API --> LNK --> LLM
  LLM --> M2
  EXT --> DB
  LNK --> DB
  API --> DB
```

The browser only ever talks to one origin: `next.config.mjs` rewrites `/api/*` to the
backend, so there is no CORS configuration to get wrong and the backend need not be
exposed.

---

## Module map

| Module | Responsibility | Depends on |
| --- | --- | --- |
| `app.py` | HTTP routes, pipeline orchestration, cancellation, startup reconciliation | everything |
| `ingest.py` | PDF → chunks; the grounding predicate | `pymupdf` |
| `extract.py` | Extraction prompt + schema; magnitude normalisation | `ingest`, `llm` |
| `link.py` | Candidate blocking; judging prompt + schema | `store`, `llm` |
| `llm.py` | One structured-JSON call per role; rate gating; retries | provider HTTP |
| `store.py` | Schema, migrations, all SQL | `sqlite3` |
| `env.py` | Loads `backend/.env` before anything reads `os.environ` | — |

Two support scripts sit alongside: `check_models.py` (preflight — one real call per role)
and `reground.py` (re-run the grounding check over stored facts).

**`llm.py` is the seam.** It is the only file that imports a provider SDK or knows one
exists; `extract.py` and `link.py` import a single function from it. That is what makes
the extraction/judging model split a configuration change rather than a rewrite, and what
made swapping the whole stack from Anthropic to Gemini + Ollama a one-file job.

---

## Pipeline flow

```mermaid
sequenceDiagram
  participant U as Browser
  participant A as app.py
  participant D as SQLite
  participant M as Model

  U->>A: POST /documents (PDF)
  A->>D: insert document (sha256 dedupe)
  A-->>U: id plus status pending
  Note over A: runs as a background task, UI polls /documents

  A->>A: ingest.parse → chunks
  A->>D: insert chunks, no_text issues
  loop per chunk, rate-gated
    A->>M: extract (JSON schema)
    M-->>A: facts + quotes
    A->>A: is_grounded(quote, chunk)
    A->>D: insert facts + issues, bump progress
  end

  loop per grounded fact, in waves
    A->>D: candidates (FTS5 + attribute + numeric)
    A->>M: judge anchor vs candidates
    M-->>A: verdicts
    A->>D: insert relations (UNIQUE a,b)
  end
  A->>D: status = done
```

Results are persisted **per unit as they land**, not gathered at the end. That is what
makes progress visible mid-run, lets a cancel keep what already succeeded, and makes a
silent API failure distinguishable from healthy work.

---

## Concurrency and pacing

```mermaid
flowchart TB
  T["N chunk tasks created at once"] --> G{"_gate per role"}
  G -->|"semaphore: max concurrent"| R["urllib POST on a worker thread"]
  G -->|"min interval = 60 / RPM"| R
  R -->|"429 / 5xx"| B["backoff, honouring Retry-After"]
  B --> R
  R -->|"429 naming a per-day or zero quota"| F["fail fast — cannot clear this run"]
  R --> OK["JSON parsed against the schema"]
```

Every task is created immediately and gated on the way out, so the gate is the single
place that knows a free tier exists. Requests-per-minute is enforced rather than
discovered — without it a run spends its time collecting 429s.

HTTP is stdlib `urllib` on a worker thread rather than an async HTTP dependency. These are
plain JSON POSTs, the gate already bounds concurrency, and it keeps the dependency list to
five packages.

---

## Incremental by construction

Adding a document extracts only its own chunks and judges only its own new facts, against
everything already stored.

- `relations` is `UNIQUE(a_id, b_id)` with the pair normalised to `a < b`, so re-running
  can never duplicate an edge.
- Already-judged pairs are excluded during candidate generation, before any model call.
- Uploading identical bytes is a no-op (SHA-256), unless the previous run produced
  nothing — then it resets and re-runs.

Nothing is ever rebuilt.

---

## Why SQLite, not a graph database

The assignment is explicit that a graph database is not the interesting part, and it is
right: the edges are cheap, the *justification* for each edge is the product.

One SQLite file gives ACID writes, full-text search (FTS5) and numeric range indexes with
zero services to run, and `relations` is a perfectly good edge table. The trade-off is
concurrency — one writer at a time, which is irrelevant for a single-user prototype.

Swapping in Postgres + pgvector is a contained change: `store.py` for the schema and
`link.candidates` for retrieval. Nothing else touches SQL.

---

## Failure handling

Every stage records what it could not do rather than dropping it silently.

| Failure | Recorded as | Effect |
| --- | --- | --- |
| Page with no extractable text | `no_text` issue | Visible blind spot; needs OCR |
| Quote not verbatim in its chunk | `ungrounded_quote` issue, `grounded=0` | Fact quarantined, never forms a relation |
| Model call fails | `llm_error` issue | That chunk is skipped, the run continues |
| Every extraction call fails | document `failed` | Retryable by re-upload |
| Server restarts mid-run | document `interrupted` at startup | No permanently "processing" rows |
| User presses Stop | document `cancelled` | Outstanding calls cancelled, results kept |
