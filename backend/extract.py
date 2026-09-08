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
import re

import ingest
from llm import LLMError, structured

# Magnitude words, lowercase. Indian and international units both appear in these
# documents, often on the same page.
_MAGNITUDES = {
    "crore": 1e7, "cr": 1e7, "crores": 1e7,
    "lakh": 1e5, "lakhs": 1e5, "lac": 1e5,
    "thousand": 1e3, "k": 1e3,
    "million": 1e6, "mn": 1e6, "mln": 1e6,
    "billion": 1e9, "bn": 1e9,
    "trillion": 1e12, "tn": 1e12,
}
_NUMBER = re.compile(r"(\()?\s*(-?)\s*(\d[\d,]*(?:\.\d+)?)")


def value_num_from_text(value_text):
    """Recompute the canonical magnitude from the value as written.

    Every model tested gets this wrong somewhere -- 8B models especially, but not
    only them: "Rs. 127 Cr" comes back as 1.27e8 instead of 1.27e9, and "30%" as
    0.3 instead of 30. It is pure arithmetic over a string, so it does not belong
    in a prompt at all. Returns None when there is no number to parse, which is
    the correct answer for a semantic fact.
    """
    if not value_text:
        return None
    for m in _NUMBER.finditer(value_text):
        paren, sign, digits = m.groups()
        # Digits glued to letters are a period token, not a value: the 23 in FY23,
        # the 3 in Q3, the 1 in H1. Skip them or they poison numeric blocking.
        start = m.start(3)
        if start > 0 and value_text[start - 1].isalpha():
            continue
        try:
            value = float(digits.replace(",", ""))
        except ValueError:
            continue
        # Accounting parentheses mean negative: "Rs. (452 Cr)" is a loss.
        if sign == "-" or (paren and ")" in value_text[m.start():]):
            value = -value

        tail = value_text[m.end():].lower()
        if tail.lstrip().startswith("%"):
            return value                   # a percentage is just its number
        for word in re.findall(r"[a-z]+", tail)[:2]:
            if word in _MAGNITUDES:
                return value * _MAGNITUDES[word]
        return value
    return None

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

PERIOD AND SCOPE ARE THE TWO MOST COMMONLY MISSED FIELDS. They are rarely written
inside the sentence you are quoting -- they come from the page around it:
  - A page or section heading like "FY24 highlights" or "Q4 FY24 results" sets the
    period for every fact on that page unless a fact states its own.
  - A table column header like "March 31, 2024" sets the period for that column.
  - A line prefixed with a segment, division or region name -- "PTL:", "Express Parcel:",
    "Rural -" -- has that as its scope.
  - Words like consolidated, standalone, provisional, revised, projected, estimated
    anywhere nearby are the scope.
Leave them null only when the document genuinely does not say. Guessing is wrong, but
so is ignoring a heading three lines up.

WORKED EXAMPLE
Excerpt (page headed "FY24 highlights"):
    TL: 40% YoY revenue growth with service EBITDA profitability improvement
Correct output for that line:
    entity      "Delhivery Limited"          <- full formal name, not "Delhivery"
    attribute   "revenue_growth"
    value_text  "40% YoY"
    value_num   40                            <- the number itself, NOT 0.4
    unit        "percent"
    period      "FY2024"                      <- inherited from the page heading
    scope       "Truckload (TL) segment"      <- from the line's own prefix
    statement   "Delhivery Limited's Truckload segment grew revenue 40% year on year
                 in FY2024."
    quote       "TL: 40% YoY revenue growth with service EBITDA profitability improvement"
    confidence  0.8                           <- period was inferred, so not 1.0

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
        data = await structured(SYSTEM, _user(doc_name, chunk), SCHEMA, role="extract")
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
        # Arithmetic beats instruction-following: trust the parsed magnitude over
        # the model's, since numeric blocking in link.py depends on it being right.
        # If value_text has digits but none of them is a value ("PAT profitable in
        # Q3 FY24"), that is a non-numeric fact however confidently the model
        # numbered it -- a spurious 3.0 is worse for blocking than no number.
        value_text = raw.get("value_text") or ""
        computed = value_num_from_text(value_text)
        if computed is not None:
            value_num = computed
        elif any(ch.isdigit() for ch in value_text):
            value_num = None
        else:
            value_num = raw.get("value_num")

        facts.append({
            "entity": (raw.get("entity") or "").strip(),
            "attribute": (raw.get("attribute") or "").strip().lower().replace(" ", "_"),
            "value_text": raw.get("value_text") or "",
            "value_num": value_num,
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
