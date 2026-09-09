# Limitations and next steps

Being precise about what has actually been run, because it changes how much weight the
rest of this carries.

---

## What has been verified

**End to end, on real documents:** PDF parsing across all six starter files (511 pages,
2.1 s), upload and duplicate rejection, the full pipeline reaching `done`, the grounding
check, candidate blocking, cancellation, every API route, and every UI tab including
source-page verification.

**Extraction and judging have run for real** against the Delhivery Q4 FY24 deck:

| | |
| --- | ---: |
| chunks | 23 |
| facts extracted | 325 |
| grounded (quote verified) | 233 |
| distinct fact types | 40 |
| relations judged | 135 |
| corroborates / reconciled / contradicts | 9 / 48 / 0 |

`python test_core.py` covers the non-LLM logic: 15 checks, all passing — page
attribution, window overlap, the grounding rule (including that a paraphrase, an altered
number and an ambiguous short quote all fail it), magnitude normalisation, candidate
blocking across crore/billion wording, idempotent relinking, progress persistence,
cancellation of outstanding work, `.env` precedence, the Gemini schema translation, and
the route SQL.

**Not yet run:** a multi-document corpus. Everything above came from one document, so
`cross_doc_relations` is 0 and the Contradictions tab is empty. Cases 1–3 want a second
document — the 2022 prospectus is the natural pair.

---

## What does not work yet

**Retrieval has a real recall hole.** A fact expressed with no shared vocabulary *and* no
shared number — a differently-written address, a role described in entirely different
words — can be missed at the candidate stage and never reach the judge. Two facts that are
never compared produce no relation, and a missing relation looks exactly like an absence
of conflict. This is the sharpest trade-off in the system.

**No OCR.** Image-only pages are reported as `no_text` issues and contribute nothing. Six
such pages across the starter set.

**Table structure is lost.** PyMuPDF returns tables as flowed text, so a figure can be
associated with the wrong column header. This is the main source of wrong-but-confident
`period` and `scope` values — the failure mode that is hardest to catch, because the field
is populated rather than absent.

**The judge sees two facts, not two documents.** It cannot use a fact three hops away to
resolve an apparent conflict, and it cannot use one document's publication date to decide
that the other is simply older.

**48 reconciled against 9 corroborated** suggests the judge reaches for `reconciled` when
`unrelated` would be right. Within a single document many facts share an attribute without
being about the same thing, and the prompt's instruction to check for a reconciling
context before declaring conflict may be pulling it too far the other way.

**Confidence is the model's self-report,** not a calibrated number. Treat it as ordering,
not probability.

**No authentication, and CORS is fully open** — a local prototype, not a deployment.

---

## Case 4: failures found, and how they are handled

### Ungrounded quotes — the model paraphrases and calls it a quotation

The failure worth showing. The model occasionally returns a smooth paraphrase, or stitches
two distant sentences together, in the `quote` field. It reads like evidence and is not.
Nothing in the prompt reliably prevents it.

It is handled outside the model. Every quote is checked against its source chunk in
Python; failures are quarantined (`grounded=0`), listed under **Failures** with the text
the model claimed, and excluded from linking so an unsupported claim can never be cited as
corroboration. 92 facts sit there now.

### A failure in my own check, found by looking at the failures

The first grounding rule required a 15-character quote. It rejected **73%** of the facts
from the earnings deck — and every one of those quotes *was* verbatim on the page. Slide
and table evidence is legitimately short (`YoY: 29.8%`, `7,054`), and the rule was
discarding it for brevity rather than for being wrong.

The fix was to change what the bar measures: ambiguity, not length. A quote under 15
characters must occur exactly once in its chunk; a longer one may repeat. Grounded facts
went 88 → 233, issues 312 → 167.

This is the failure I would most want a reviewer to look at, because the system's own
Failures tab is what surfaced it — the facts were sitting there, visibly wrong, with the
evidence attached.

### Numeric normalisation, moved out of the model entirely

Every model tested mis-scaled magnitudes: `Rs. 127 Cr` as 1.27e8 instead of 1.27e9, `30%`
as 0.3. Numeric blocking depends on that value, so it is now recomputed in Python from
`value_text`. The first version of *that* introduced its own bug — `PAT profitable in Q3`
parsed as 3.0 — so it refuses digits glued to letters, since a spurious number is worse
for blocking than none.

### Everything else the pipeline knows it got wrong

`no_text` pages, `llm_error` calls with the HTTP status and body, and `pipeline_error`
exceptions all land in the same Failures tab. The system's blind spots are a visible
feature rather than a silent gap.

---

## What I would build next

1. **Close the recall hole** — add embeddings as a *fourth* candidate signal beside the
   existing three. `link.candidates` is already the single place to change, and the union
   pattern means it slots in without touching anything else.
2. **Cluster instead of pair.** Group facts by (entity, attribute) into a claim with many
   sources and one timeline, rather than judging pairs independently. This is what would
   let "resigned in a later document" resolve automatically from publication dates, and
   would likely fix the reconciled/unrelated imbalance.
3. **Table-aware extraction** — feed page images for pages PyMuPDF flags as table-dense,
   so column headers survive and `period`/`scope` stop being guessed.
4. **A grounded eval set** — a few dozen hand-labelled pairs from these documents, so
   prompt changes can be measured instead of eyeballed. Every improvement above is
   currently judged by reading output, which does not scale.
5. **Batch API for bulk ingest** — 50% cheaper on providers that offer it, and the
   per-chunk call is already independent and shaped for it.
