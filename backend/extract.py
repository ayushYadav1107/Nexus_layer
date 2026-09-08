"""Fact extraction: one chunk in, grounded facts out.

Two ideas do most of the work here:

1. The model must return a verbatim `quote`. We then check that quote against
   the chunk in Python (`ingest.is_grounded`). Facts whose quote does not
   survive that check are stored with grounded=0 and logged as an issue -- they
   are never allowed to form relations. This is the cheap deterministic guard
   against a paraphrase dressed up as evidence.

2. Normalisation happens at extraction time, not at match time. The model emits
   a canonical `entity`/`attribute` and a `value_num` in base units alongside
   the text as written. That is what lets "Revenue from operations of Rs 7,225
   crore" and "topline of INR 72.25 billion" find each other later without an
   embedding model.
"""
import ingest
from llm import LLMError, structured

SYSTEM = """You extract atomic, checkable facts from one excerpt of a business or \
institutional document, for a knowledge layer that later cross-references facts \
across many documents.

WHAT COUNTS AS A FACT
A fact is a single claim about a single subject that a careful reader could verify \
against this excerpt. Extract both:
  - numerical facts (revenue, growth rates, headcount, dates, ratios, counts, prices)
  - semantic facts (roles and appointments, ownership, status, locations, definitions,
    stated policies, forward guidance attributed to someone)

Do NOT extract:
  - navigation and boilerplate: table-of-contents lines, page headers/footers, legal
    disclaimers, "this page intentionally left blank"
  - vague or unfalsifiable claims ("we are committed to excellence", "strong momentum")
  - anything you cannot support with words that literally appear in the excerpt

Prefer fewer, high-quality facts over exhaustive coverage. A page of dense financial \
tables may yield many facts; a page of prose may yield two or three, or none. \
Returning an empty list is a correct answer for boilerplate pages.

FIELDS
entity      The subject, written the same way you would write it every time you meet it.
            Use the full formal name when the excerpt gives one ("Delhivery Limited",
            "India", "Reserve Bank of India"). Not a pronoun, not "the Company".
attribute   A canonical snake_case name for what is being asserted about the entity,
            chosen so the SAME real-world measure gets the SAME string in any document:
            revenue_from_operations, net_profit, ebitda_margin, headcount,
            real_gdp_growth, cpi_inflation, board_member_role, registered_address.
            Reuse an obvious standard name rather than inventing a document-specific one.
value_text  The value exactly as the document expresses it, including its unit and
            magnitude words: "Rs. 7,225.29 million", "6.4 per cent", "Sandeep Kumar
            Barasia, Executive Director".
value_num   The same value as a plain number in BASE units, or null if not numeric.
            Expand magnitude words: 7,225.29 million -> 7225290000. Crore -> x10^7,
            lakh -> x10^5, billion -> x10^9. A percentage is just its number: 6.4.
            A ratio like 1.35x is 1.35. Never carry the magnitude word into value_num.
unit        Canonical unit for value_num: INR, USD, percent, percentage_points, count,
            days, years, ratio, index_points, tonnes, sq_ft. null if not numeric.
period      The time the fact refers to, normalised: "FY2024", "Q4 FY2024", "CY2023",
            "2024-25", "as of 2024-03-31". null if the claim is timeless. If the
            excerpt only implies a period from a table header or column, still record it.
scope       The qualification that bounds the claim: "consolidated", "standalone",
            "India", "excluding Spoton", "rural", "provisional estimate", "IMF staff
            projection". null if unqualified. THIS FIELD MATTERS: two different numbers
            for the same attribute are usually a scope difference, not an error.
qualifiers  Any further conditions as key/value pairs -- basis, source attribution,
            revision status, segment, comparison base. Invent whatever keys the
            document's own vocabulary suggests; this field is deliberately open.
statement   One self-contained sentence restating the fact so it is understandable
            with no other context. Include entity, period and scope.
quote       A VERBATIM span copied character-for-character from the excerpt that
            supports the fact. It must appear in the excerpt exactly as you write it.
            Copy, never paraphrase, never join two distant sentences, never fix a typo
            or normalise a number. 15-300 characters. If you cannot copy such a span,
            do not emit the fact at all.
confidence  0.0-1.0. Lower it when the period or scope had to be inferred from layout,
            when the text is a garbled table, or when the subject is ambiguous.

Return only the JSON object."""

SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string"},
                    "attribute": {"type": "string"},
                    "value_text": {"type": "string"},
                    "value_num": {"type": ["number", "null"]},
                    "unit": {"type": ["string", "null"]},
                    "period": {"type": ["string", "null"]},
                    "scope": {"type": ["string", "null"]},
                    "qualifiers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"key": {"type": "string"},
                                           "value": {"type": "string"}},
                            "required": ["key", "value"],
                            "additionalProperties": False,
                        },
                    },
                    "statement": {"type": "string"},
                    "quote": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["entity", "attribute", "value_text", "value_num", "unit",
                             "period", "scope", "qualifiers", "statement", "quote",
                             "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["facts"],
    "additionalProperties": False,
}


def _user(doc_name, chunk):
    where = f"page {chunk['page']}"
    if chunk.get("page_label"):
        where += f" (printed page {chunk['page_label']})"
    return (f"Document: {doc_name}\nLocation: {where}\n\n"
            f"<excerpt>\n{chunk['text']}\n</excerpt>\n\n"
            "Extract the facts in this excerpt.")


async def extract_chunk(doc_name, chunk):
    """Returns (facts, issues). Never raises -- a bad chunk becomes an issue."""
    try:
        data = await structured(SYSTEM, _user(doc_name, chunk), SCHEMA, effort="medium")
    except LLMError as e:
        return [], [{"kind": "llm_error", "detail": str(e), "page": chunk["page"]}]

    facts, issues = [], []
    for raw in data.get("facts", []):
        quote = (raw.get("quote") or "").strip()
        grounded = ingest.is_grounded(quote, chunk["text"])
        if not grounded:
            # Kept, but quarantined: visible in the UI as a failure case, excluded
            # from linking so an unsupported claim can never become "evidence".
            issues.append({
                "kind": "ungrounded_quote",
                "detail": f"quote not found verbatim on page {chunk['page']}: "
                          f"{raw.get('attribute')} = {raw.get('value_text')}",
                "page": chunk["page"],
                "payload": {"quote": quote, "statement": raw.get("statement")},
            })
        facts.append({
            "entity": (raw.get("entity") or "").strip(),
            "attribute": (raw.get("attribute") or "").strip().lower().replace(" ", "_"),
            "value_text": raw.get("value_text") or "",
            "value_num": raw.get("value_num"),
            "unit": raw.get("unit"),
            "period": raw.get("period"),
            "scope": raw.get("scope"),
            "qualifiers": {q["key"]: q["value"] for q in (raw.get("qualifiers") or [])
                           if isinstance(q, dict) and "key" in q},
            "statement": raw.get("statement") or "",
            "quote": quote,
            "page": chunk["page"],
            "page_label": chunk.get("page_label"),
            "grounded": grounded,
            "confidence": raw.get("confidence"),
        })
    facts = [f for f in facts if f["entity"] and f["attribute"] and f["statement"]]
    return facts, issues
