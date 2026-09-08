"""Cross-referencing: find candidate pairs cheaply, then judge them carefully.

Retrieval is deliberately not an embedding index. Normalisation already happened
at extraction time (canonical `entity`/`attribute`, `value_num` in base units), so
three cheap lexical/numeric signals recover almost all of the recall an embedding
model would, with no extra service, no vector store, and a query you can read:

  1. FTS5 (porter-stemmed) over entity + attribute + statement + value_text
  2. exact match on the canonical attribute
  3. numeric near-match on value_num within the same unit -- this is the path that
     catches "Rs 7,225 crore" against "INR 72.25 billion", where the words share
     nothing but the magnitude agrees

The trade-off is honest: a fact phrased with no shared vocabulary AND no shared
number will be missed. See README "Limitations".

Judging is batched -- one call per new fact covering all of its candidates -- which
is what keeps a 300-page corpus to hundreds of calls rather than thousands.
"""
import json
import re

from llm import LLMError, structured
from store import db

STOP = {"the", "and", "for", "with", "was", "were", "has", "have", "had", "its", "from",
        "that", "this", "are", "per", "cent", "year", "over", "than", "which", "been",
        "during", "under", "into", "also", "such", "their", "there", "these", "those",
        "total", "value", "number", "rate"}

FTS_LIMIT = 10
NUM_TOLERANCE = 0.02
MAX_CANDIDATES = 8

SYSTEM = """You compare pairs of facts extracted from different parts of a document \
corpus and decide how each pair relates. Each fact carries its source document, page, \
verbatim quote, and the context fields that bound it: period, scope, unit, qualifiers.

You are given ONE anchor fact and several candidate facts. Classify the anchor against \
each candidate independently.

LABELS
"corroborates" - both facts assert the same thing about the same entity, and both can be
    true at once. Different wording, different units, or different magnitudes of the same
    number all still corroborate (7,225.29 million rupees and 722.53 crore are the same
    amount). Rounding differences and "approximately" phrasing still corroborate.

"contradicts" - the facts are about the same entity, the same measure, the SAME period
    and the SAME scope, and cannot both be true. A director described as currently
    serving in one document and as having resigned on an earlier date in another. Two
    incompatible figures for one measure with nothing in either fact that would explain
    the gap. Use this when you have checked for a reconciling context and did not find one.

"reconciled" - the two facts look contradictory on their surface but are both plausibly
    true once a specific stated difference is taken into account. You MUST name that
    difference in `resolution`, and it must be visible in the fact fields or quotes --
    not invented. Legitimate reasons include: different periods (FY2023 vs FY2024, full
    year vs quarter), different scope (consolidated vs standalone, with vs without an
    acquired entity, national vs rural), different units or magnitudes, different
    vintages of the same estimate (provisional vs revised vs projection), different
    publishers measuring on different definitions, or a point-in-time status that changed
    between the two documents' dates.

"unrelated" - anything else, including facts about different entities, different
    measures, or merely adjacent topics. This is the correct and expected answer for
    most pairs. Do not stretch to find a relationship.

RULES
- Never label "contradicts" before checking period, scope, unit and publication date. A
  differing number with a differing period is "reconciled", not a contradiction.
- Never label "corroborates" for two facts that merely both sound positive about the
  same entity. They must assert the same measure.
- `reasoning` is one or two sentences, and must refer to the concrete values, periods or
  scopes involved -- not restate the label.
- `resolution` is non-null only for "reconciled": state plainly what dimension differs
  and why both readings stand.
- confidence 0.0-1.0. Be genuinely uncertain when the quotes are thin or a period had to
  be assumed.

Return only the JSON object."""

SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "integer"},
                    "relationship": {"type": "string",
                                     "enum": ["corroborates", "contradicts",
                                              "reconciled", "unrelated"]},
                    "confidence": {"type": "number"},
                    "reasoning": {"type": "string"},
                    "resolution": {"type": ["string", "null"]},
                },
                "required": ["candidate_id", "relationship", "confidence",
                             "reasoning", "resolution"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


def _fts_query(fact):
    words = re.findall(r"[a-z0-9]+", f"{fact['entity']} {fact['attribute']} {fact['statement']}".lower())
    seen, terms = set(), []
    for w in words:
        if len(w) < 3 or w in STOP or w in seen:
            continue
        seen.add(w)
        terms.append(f'"{w}"')
        if len(terms) >= 14:
            break
    return " OR ".join(terms)


def candidates(fact_id):
    """Candidate fact rows for `fact_id`, already filtered of self, same-chunk and
    previously-judged pairs. Cheap: three indexed queries, no model call."""
    with db() as con:
        f = con.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
        if f is None or not f["grounded"]:
            return []

        ordered = []
        q = _fts_query(f)
        if q:
            try:
                ordered += [r["id"] for r in con.execute(
                    """SELECT f.id FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid
                       WHERE facts_fts MATCH ? AND f.id != ? AND f.grounded = 1
                       ORDER BY bm25(facts_fts) LIMIT ?""",
                    (q, fact_id, FTS_LIMIT)).fetchall()]
            except Exception:
                pass  # malformed FTS expression: fall through to the other two signals

        ordered += [r["id"] for r in con.execute(
            "SELECT id FROM facts WHERE attribute=? AND id!=? AND grounded=1 LIMIT ?",
            (f["attribute"], fact_id, FTS_LIMIT)).fetchall()]

        if f["value_num"] is not None and f["unit"]:
            v = f["value_num"]
            eps = max(abs(v) * NUM_TOLERANCE, 1e-9)
            ordered += [r["id"] for r in con.execute(
                """SELECT id FROM facts WHERE unit=? AND value_num BETWEEN ? AND ?
                   AND id!=? AND grounded=1 LIMIT ?""",
                (f["unit"], v - eps, v + eps, fact_id, FTS_LIMIT)).fetchall()]

        judged = {r["a_id"] if r["b_id"] == fact_id else r["b_id"] for r in con.execute(
            "SELECT a_id,b_id FROM relations WHERE a_id=? OR b_id=?", (fact_id, fact_id))}

        picked, out = set(), []
        for cid in ordered:
            if cid in picked or cid in judged:
                continue
            row = con.execute("SELECT * FROM facts WHERE id=?", (cid,)).fetchone()
            if row is None or row["chunk_id"] == f["chunk_id"]:
                continue
            picked.add(cid)
            out.append(dict(row))
            if len(out) >= MAX_CANDIDATES:
                break
        return out


def _render(f, doc_name, label=None):
    where = f"{doc_name}, page {f['page']}"
    if f["page_label"]:
        where += f" (printed page {f['page_label']})"
    value = f["value_text"]
    if f["value_num"] is not None:
        value += f"   [normalised: {f['value_num']} {f['unit']}]"
    quals = json.loads(f["qualifiers"] or "{}")
    lines = [
        f"[{label}]" if label else "",
        f"source: {where}",
        f"entity: {f['entity']}",
        f"attribute: {f['attribute']}",
        f"value: {value}",
        f"period: {f['period']}",
        f"scope: {f['scope']}",
        f"qualifiers: {json.dumps(quals) if quals else 'none'}",
        f"statement: {f['statement']}",
        f"quote: {f['quote']!r}",
    ]
    return "\n".join(line for line in lines if line)


def _doc_names():
    with db() as con:
        return {r["id"]: r["filename"] for r in con.execute("SELECT id, filename FROM documents")}


async def judge(fact_id):
    """Judge one fact against its candidates. Returns (verdicts, issues)."""
    cands = candidates(fact_id)
    if not cands:
        return [], []
    with db() as con:
        anchor = dict(con.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone())
    names = _doc_names()

    parts = ["ANCHOR FACT", _render(anchor, names.get(anchor["doc_id"], "?")), "", "CANDIDATES"]
    for c in cands:
        parts.append(_render(c, names.get(c["doc_id"], "?"), label=f"candidate_id={c['id']}"))
        parts.append("")
    parts.append("Classify the anchor against each candidate. Return one verdict per candidate_id.")
    user = "\n".join(parts)

    try:
        data = await structured(SYSTEM, user, SCHEMA, role="judge", max_tokens=4000)
    except LLMError as e:
        return [], [{"kind": "llm_error", "detail": f"judge fact {fact_id}: {e}",
                     "page": anchor["page"]}]

    by_id = {c["id"]: c for c in cands}
    out = []
    for v in data.get("verdicts", []):
        c = by_id.get(v.get("candidate_id"))
        if c is None:
            continue
        out.append({
            "a_id": fact_id, "b_id": c["id"],
            "kind": v["relationship"],
            "confidence": v.get("confidence"),
            "reasoning": v.get("reasoning") or "",
            "resolution": v.get("resolution"),
            "cross_doc": c["doc_id"] != anchor["doc_id"],
        })
    return out, []
