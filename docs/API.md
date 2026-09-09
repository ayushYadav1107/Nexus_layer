# API

FastAPI on `:8000`. Interactive schema at `/docs`. The Next.js dev server proxies
`/api/*` to it, so the browser only ever talks to one origin.

---

## Documents

### `POST /documents`

Multipart upload, field name `file`. Validates the PDF magic bytes and the size cap, then
starts a background task and returns immediately.

| Response | Meaning |
| --- | --- |
| `{"id": 3, "status": "pending"}` | Accepted; poll `GET /documents` for progress |
| `{"id": 3, "status": "duplicate", "detail": "already ingested — 233 facts from this file"}` | Same bytes, and the previous run produced facts |
| `400` / `413` / `415` | Empty, too large, or not a PDF |

Uploads are deduplicated by SHA-256. Re-uploading a document whose run **failed, was
cancelled, was interrupted, or finished with no facts** resets it and runs again — that is
the retry path.

### `GET /documents`

Every document with per-document counts and live progress.

```json
[{ "id": 2, "filename": "deck.pdf", "status": "extracting", "n_pages": 27,
   "done": 14, "total": 23, "error": null,
   "n_facts": 180, "n_ungrounded": 40, "n_issues": 44 }]
```

`done`/`total` are progress **within the current phase** — chunks while extracting, facts
while linking. Written per unit as work lands, not at the end.

### `POST /documents/{id}/cancel`

Stops an in-flight run. Facts already extracted are kept; outstanding calls are cancelled.

| Response | Meaning |
| --- | --- |
| `{"id": 3, "status": "cancelling"}` | Cancellation requested |
| `409` | Not currently being processed — the row's status is stale, not the button |

---

## Facts

### `GET /facts`

| Parameter | Default | Effect |
| --- | --- | --- |
| `q` | — | FTS5 search over entity, attribute, statement, value |
| `doc_id` | all | Restrict to one document |
| `grounded` | all | `1` verified, `0` quarantined |
| `limit` | 200 | Capped at 1000 |

Returns full fact rows plus `filename`, with `qualifiers` parsed from JSON.

### `GET /facts/{id}`

One fact, **the full text of the chunk it came from**, and every non-`unrelated` relation
it participates in.

```json
{ "fact": { … },
  "evidence_context": "…the entire page text…",
  "relations": [{ "relation_id": 9, "kind": "corroborates", "confidence": 0.93,
                  "reasoning": "…", "resolution": null, "cross_doc": 1,
                  "other_id": 41, "other_doc": "annual-report.pdf",
                  "other_page": 37, "other_quote": "…" }] }
```

This is what backs "verify on source page" in the UI — the page is rendered with the quote
highlighted in place, so a reader can check the evidence rather than trust it.

---

## Relations

### `GET /relations`

The main cross-reference view. `unrelated` is never returned.

| Parameter | Effect |
| --- | --- |
| `kind` | `corroborates` \| `contradicts` \| `reconciled` — one of the four demo cases |
| `doc_id` | Relations touching that document on either side |
| `cross_doc` | `1` across documents, `0` within one |
| `min_confidence` | Floor on the judge's self-reported confidence |
| `limit` | Capped at 1000 |

Each row is flattened for display: `a_*` and `b_*` carry both sides' document, page,
entity, attribute, value, period, scope and quote, alongside `kind`, `confidence`,
`reasoning` and `resolution`. Ordered cross-document first, then by confidence.

---

## Issues

### `GET /issues`

Everything the pipeline knows it got wrong or could not do — the fourth required case.

| `kind` | Meaning |
| --- | --- |
| `no_text` | Page had no extractable text; needs OCR |
| `ungrounded_quote` | Quote not found verbatim; `payload.quote` holds what the model claimed |
| `llm_error` | The model call failed; the detail carries the HTTP status and body |
| `pipeline_error` | An unexpected exception, with type and message |

---

## Stats

### `GET /stats`

Powers the header, the funnel, both panels and the document cards.

```json
{ "documents": 2, "facts": 325, "grounded": 233, "issues": 167,
  "cross_doc_relations": 0,
  "relations": { "corroborates": 9, "reconciled": 48, "unrelated": 78 },
  "models": { "extract": "gemini:gemini-3.5-flash-lite",
              "judge": "gemini:gemini-2.5-flash" },
  "attributes": [{ "attribute": "adjusted_ebitda", "n": 30 }, …],
  "per_document": [{ "id": 2, "filename": "deck.pdf", "status": "done",
                     "n_pages": 27, "n_chunks": 23,
                     "n_grounded": 233, "n_ungrounded": 92, "n_issues": 167 }] }
```

`models` reports which model each role actually resolved to, so results always say what
produced them. `attributes` is the dynamic schema — the vocabulary as it has accumulated,
declared nowhere.
