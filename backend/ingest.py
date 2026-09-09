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

# Chars per chunk -- the biggest lever on run time, since extraction is one call
# per chunk. Page boundaries are never crossed either way.
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


MIN_QUOTE = 4
UNAMBIGUOUS_AT = 15


def is_grounded(quote, chunk_text):
    """True when the quote appears verbatim (modulo whitespace) in the chunk.

    The bar is ambiguity, not length: slide and table evidence is legitimately
    short ("YoY: 29.8%"), so a quote under UNAMBIGUOUS_AT must occur exactly once
    in the chunk; a longer one may repeat.
    """
    q = normalise(quote)
    if len(q) < MIN_QUOTE:
        return False
    t = normalise(chunk_text)
    if q not in t:
        return False
    return len(q) >= UNAMBIGUOUS_AT or t.count(q) == 1
