"""PDF -> page-anchored text chunks.

Chunks never span pages. That costs a little context at page boundaries but
buys exact page attribution for every quote, which is the whole point of the
grounding layer. Windows within a long page overlap so a sentence split across
the window boundary still appears whole in one of them.
"""
import os
import re

import pymupdf

import env  # noqa: F401  loads .env before the reads below

# Chars per chunk. This is the single biggest lever on how long a run takes,
# because extraction is one model call per chunk: at 4000 most pages of a dense
# report split in two, at 8000 most fit whole. Bigger chunks mean fewer calls and
# no split context, at the cost of asking the model to read more at once -- worth
# it on any model with a large context window. Page boundaries are still never
# crossed, so page attribution is unaffected either way.
WINDOW = int(os.environ.get("FACTLAYER_CHUNK_CHARS", "4000"))
OVERLAP = min(400, WINDOW // 10)
MIN_TEXT = 80      # below this a page is treated as having no extractable text


def _clean(s):
    s = s.replace("­", "")                 # soft hyphens
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _windows(text):
    """Split one page into overlapping windows, preferring paragraph breaks."""
    if len(text) <= WINDOW:
        return [text]
    out, start = [], 0
    while start < len(text):
        end = min(start + WINDOW, len(text))
        if end < len(text):
            brk = text.rfind("\n", start + WINDOW // 2, end)
            if brk > start:
                end = brk
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - OVERLAP, start + 1)
    return [w for w in out if w]


def parse(pdf_bytes):
    """Returns (chunks, empty_pages, n_pages).

    chunks: [{page, page_label, ordinal, text}]
    empty_pages: pages with no extractable text -- scanned images, or figures.
                 Surfaced as issues rather than silently dropped.
    """
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    chunks, empty = [], []
    try:
        for i, page in enumerate(doc, start=1):
            text = _clean(page.get_text("text"))
            if len(text) < MIN_TEXT:
                empty.append(i)
                continue
            label = None
            try:
                label = page.get_label() or None      # number printed in the document
            except Exception:
                pass
            for j, w in enumerate(_windows(text)):
                chunks.append({"page": i, "page_label": label, "ordinal": j, "text": w})
        return chunks, empty, doc.page_count
    finally:
        doc.close()


def normalise(s):
    """Whitespace/quote-insensitive form used to verify a quote against its chunk."""
    s = s.replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def is_grounded(quote, chunk_text):
    """True when the quote appears verbatim (modulo whitespace) in the chunk.

    This is the cheap deterministic check that catches the most common LLM
    failure here: a paraphrase presented as a quotation.
    """
    q = normalise(quote)
    return len(q) >= 15 and q in normalise(chunk_text)
