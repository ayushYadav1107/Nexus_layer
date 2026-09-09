# Nexus Layer

A fact knowledge layer for PDFs. It extracts atomic facts, **pins every one to a verbatim
quote on a specific page**, and cross-references each new fact against everything already
stored — deciding whether a pair **corroborates**, **contradicts**, or can be
**reconciled by context** (period, scope, units, vintage).

Built for the Superjoin VIT 2026 engineering intern assignment.

```
PDF ──► page-anchored chunks ──► grounded facts ──► cross-references ──► UI
```

---

## Quick start

Requires Python 3.11+, Node 18+, and a free [Gemini API key](https://aistudio.google.com/apikey).

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env          # then paste your key on the GEMINI_API_KEY= line
```

Verify both models answer before ingesting anything — this takes five seconds and
saves an hour:

```bash
python check_models.py
```

```bash
uvicorn app:app --port 8000
```

In a second terminal:

```bash
cd web && npm install && npm run dev
```

Open <http://localhost:3000> and upload a PDF. API docs are at <http://localhost:8000/docs>.

**Start with one small document.** Extraction is one model call per chunk and the free
tier is rate-limited — see [Configuration](docs/CONFIGURATION.md#cost-and-pacing).

Run the self-check (no API key, no network, no test framework — 15 checks):

```bash
cd backend && python test_core.py
```

---

## Documentation

| Document | What it covers |
| --- | --- |
| [Architecture](docs/ARCHITECTURE.md) | Module map, request and pipeline flow, why SQLite |
| [Data model](docs/DATA-MODEL.md) | Every table and column, ER diagram, why the schema is shaped this way |
| [Pipeline](docs/PIPELINE.md) | Ingestion, extraction, grounding, candidate blocking, judging |
| [API](docs/API.md) | Every endpoint with parameters and response shapes |
| [Configuration](docs/CONFIGURATION.md) | Models, keys, rate limits, cost, tuning |
| [Deployment](docs/DEPLOYMENT.md) | Running it beyond localhost, and what the architecture rules out |
| [Limitations](docs/LIMITATIONS.md) | What does not work, measured results, what I would build next |

---

## Video Demo

*(link here)* — 3 minutes, showing a PDF being processed and the four required cases.

---

## The four required cases

Each is a tab in the UI, backed by `GET /relations?kind=…` and `GET /issues`.

| # | Case | Where |
| --- | --- | --- |
| 1 | Corroborated across documents, expressed differently | **Corroborated** tab, "across documents" |
| 2 | Genuine or likely contradiction | **Contradictions** tab |
| 3 | Apparent contradiction explained by context | **Reconciled** tab — each card names the dimension that resolves it |
| 4 | An extraction or reasoning failure, and how it is handled | **Failures** tab |

For cases 1–3 the card shows both facts side by side with their source document, page,
period, scope and verbatim quote, plus the judge's reasoning. Every quote has a **"verify
on source page"** control that renders the full page text with the quote highlighted in
place — a grounding claim you cannot check is just another assertion.

Case 4 is covered in depth in [Limitations](docs/LIMITATIONS.md#case-4-failures-found-and-how-they-are-handled).

---

## Approach in one page

**Grounding is verified in Python, not trusted.** The model must return a span copied
character-for-character from the chunk; `ingest.is_grounded` then checks it against the
source text. A fact whose quote does not survive is stored with `grounded=0`, surfaced as
a failure, and **never allowed to form a relation**. This is the cheap deterministic guard
against the failure mode that matters here: a fluent paraphrase presented as evidence.

**Normalisation happens at extraction time, not at match time.** The model emits a
canonical `attribute` and the value as written; `value_num` is then recomputed in Python
from the text, because every model tested mis-scaled crore and percentages. That pushes
the hard part of matching upstream and lets retrieval stay simple.

**`period` and `scope` are first-class columns**, because they are what decides whether
two different numbers are a contradiction or an artefact of reporting basis. Extracting
them *before* comparison is what makes reconciliation possible at all.

**Candidate generation is deliberately not a vector index** — FTS5, exact attribute match,
and numeric near-match on the normalised magnitude. The third is what links
*"Rs. 722.53 crore"* to *"INR 7.2253 billion"*, where the words share nothing. The honest
cost of this choice is in [Limitations](docs/LIMITATIONS.md).

**Nothing is document-specific.** No filename, company, or fact type appears in the code,
schema or prompts. `attribute` is not an enum — it accumulates as documents introduce new
kinds of fact, and `GET /stats` reports the vocabulary as it grows.

### AI tools used

Built with Claude Code (Claude Opus 5). The pipeline calls **Gemini** through a
provider-agnostic wrapper; `llm.py` is the only file that knows a provider exists, so
extraction and judging can point at Gemini, a local Ollama model, or Anthropic
independently.

---

## Additional notes

- No credentials in the repository. `.env` is gitignored; `.env.example` is the template.
- `factlayer.db` is local state and gitignored. Delete it to reset the layer.
- The starter PDFs are not committed — download them from the assignment and upload
  through the UI.
- `reground.py` re-runs the grounding check over an existing database when the rule
  changes, without paying for extraction again.
