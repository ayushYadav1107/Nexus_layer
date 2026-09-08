"""SQLite storage for the fact knowledge layer.

Design notes (see README): one file, WAL, integer PKs so the FTS5
external-content table can hang off `facts.rowid` directly. The only "graph"
here is the `relations` edge table -- deliberately thin, because the
interesting part is how edges are justified, not how they are stored.
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

import env  # noqa: F401  loads .env before the reads below

DB_PATH = os.environ.get("FACTLAYER_DB", os.path.join(os.path.dirname(__file__), "factlayer.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  id          INTEGER PRIMARY KEY,
  filename    TEXT NOT NULL,
  sha256      TEXT NOT NULL UNIQUE,
  n_pages     INTEGER,
  status      TEXT NOT NULL DEFAULT 'pending',   -- pending|parsing|extracting|linking|done|failed|cancelled
  error       TEXT,
  created_at  REAL NOT NULL,
  done        INTEGER NOT NULL DEFAULT 0,        -- units finished in the current phase
  total       INTEGER NOT NULL DEFAULT 0         -- units in the current phase
);

CREATE TABLE IF NOT EXISTS chunks (
  id          INTEGER PRIMARY KEY,
  doc_id      INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  page        INTEGER NOT NULL,        -- 1-based physical page in the uploaded PDF
  page_label  TEXT,                    -- page number printed in the document, if any
  ordinal     INTEGER NOT NULL,        -- nth window within that page
  text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

-- A fact is (entity, attribute, value) plus the context that decides whether
-- two values may legitimately differ: period, scope, unit. `qualifiers` is
-- free-form JSON so the schema grows new dimensions without a migration.
CREATE TABLE IF NOT EXISTS facts (
  id          INTEGER PRIMARY KEY,
  doc_id      INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  chunk_id    INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
  entity      TEXT NOT NULL,
  attribute   TEXT NOT NULL,
  value_text  TEXT NOT NULL,
  value_num   REAL,                    -- canonical magnitude in base units, NULL if non-numeric
  unit        TEXT,                    -- 'INR' | 'percent' | 'count' | ... NULL if non-numeric
  period      TEXT,
  scope       TEXT,
  qualifiers  TEXT NOT NULL DEFAULT '{}',
  statement   TEXT NOT NULL,           -- self-contained restatement, used for search + UI
  quote       TEXT NOT NULL,           -- verbatim span from the chunk
  page        INTEGER NOT NULL,
  page_label  TEXT,
  grounded    INTEGER NOT NULL DEFAULT 0,
  confidence  REAL,
  created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_doc ON facts(doc_id);
CREATE INDEX IF NOT EXISTS idx_facts_num ON facts(value_num);
CREATE INDEX IF NOT EXISTS idx_facts_attr ON facts(attribute);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
  entity, attribute, statement, value_text,
  content='facts', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, entity, attribute, statement, value_text)
  VALUES (new.id, new.entity, new.attribute, new.statement, new.value_text);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, entity, attribute, statement, value_text)
  VALUES ('delete', old.id, old.entity, old.attribute, old.statement, old.value_text);
END;

-- One row per judged pair. UNIQUE(a,b) with a<b normalised by the caller makes
-- linking idempotent, which is what lets a new document be added without
-- re-judging anything that already exists.
CREATE TABLE IF NOT EXISTS relations (
  id          INTEGER PRIMARY KEY,
  a_id        INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
  b_id        INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,           -- corroborates|contradicts|reconciled|unrelated
  confidence  REAL,
  reasoning   TEXT NOT NULL,
  resolution  TEXT,                    -- for 'reconciled': the context that explains the gap
  cross_doc   INTEGER NOT NULL DEFAULT 0,
  created_at  REAL NOT NULL,
  UNIQUE(a_id, b_id)
);
CREATE INDEX IF NOT EXISTS idx_rel_kind ON relations(kind);
CREATE INDEX IF NOT EXISTS idx_rel_a ON relations(a_id);
CREATE INDEX IF NOT EXISTS idx_rel_b ON relations(b_id);

-- Case 4: everything the pipeline knows it got wrong or could not do.
CREATE TABLE IF NOT EXISTS issues (
  id          INTEGER PRIMARY KEY,
  doc_id      INTEGER REFERENCES documents(id) ON DELETE CASCADE,
  page        INTEGER,
  kind        TEXT NOT NULL,           -- no_text|ungrounded_quote|llm_error|bad_json|...
  detail      TEXT NOT NULL,
  payload     TEXT,
  created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_issues_doc ON issues(doc_id);
"""


@contextmanager
def db():
    # ponytail: connection per call + WAL. Swap for a pool if writers ever exceed a handful.
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
    finally:
        con.close()


def init():
    with db() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS will not add columns to a database made by an
        # earlier version, so bring those forward explicitly.
        have = {r["name"] for r in con.execute("PRAGMA table_info(documents)")}
        for col, decl in (("done", "INTEGER NOT NULL DEFAULT 0"),
                          ("total", "INTEGER NOT NULL DEFAULT 0")):
            if col not in have:
                con.execute(f"ALTER TABLE documents ADD COLUMN {col} {decl}")


def add_document(filename, sha256):
    """Returns (doc_id, is_new). Re-uploading identical bytes is a no-op."""
    with db() as con:
        row = con.execute("SELECT id FROM documents WHERE sha256=?", (sha256,)).fetchone()
        if row:
            return row["id"], False
        cur = con.execute(
            "INSERT INTO documents(filename, sha256, status, created_at) VALUES (?,?,'pending',?)",
            (filename, sha256, time.time()),
        )
        return cur.lastrowid, True


def set_doc_status(doc_id, status, error=None, n_pages=None):
    with db() as con:
        con.execute(
            "UPDATE documents SET status=?, error=COALESCE(?,error), n_pages=COALESCE(?,n_pages) WHERE id=?",
            (status, error, n_pages, doc_id),
        )


LIVE_STATUSES = ("pending", "parsing", "extracting", "linking")


def mark_orphans():
    """Fail documents left mid-run by a process that is no longer alive.

    A run only exists inside the process that started it, so anything still in a
    live status at startup was orphaned by a crash, a restart or a Ctrl+C. Left
    alone the UI shows it processing forever, and Stop answers 409 because there
    is no task to cancel. Returns how many were reconciled.
    """
    marks = ",".join("?" * len(LIVE_STATUSES))
    with db() as con:
        n = con.execute(
            f"UPDATE documents SET status='interrupted', done=0, total=0, "
            f"error='interrupted -- the server restarted mid-run; re-upload to resume' "
            f"WHERE status IN ({marks})", LIVE_STATUSES).rowcount
    return n


def reset_document(doc_id):
    """Clear a document's derived data so it can be ingested again.

    Chunks cascade to facts and facts cascade to relations, so this leaves the
    document row and drops everything downstream of it. Needed because uploads are
    deduplicated by content hash: without it, a run that failed or was cancelled
    could never be retried without deleting the whole database.
    """
    with db() as con:
        con.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        con.execute("DELETE FROM issues WHERE doc_id=?", (doc_id,))
        con.execute("UPDATE documents SET status='pending', error=NULL, done=0, total=0 "
                    "WHERE id=?", (doc_id,))


def set_progress(doc_id, done, total):
    """Progress within the current phase. Written as work completes, not at the
    end -- a run that shows nothing for 45 minutes is indistinguishable from a
    run that is silently failing, which is exactly how this went wrong once."""
    with db() as con:
        con.execute("UPDATE documents SET done=?, total=? WHERE id=?", (done, total, doc_id))


def add_chunks(doc_id, chunks):
    with db() as con:
        ids = []
        for c in chunks:
            cur = con.execute(
                "INSERT INTO chunks(doc_id, page, page_label, ordinal, text) VALUES (?,?,?,?,?)",
                (doc_id, c["page"], c.get("page_label"), c["ordinal"], c["text"]),
            )
            ids.append(cur.lastrowid)
        return ids


def add_fact(doc_id, chunk_id, f):
    with db() as con:
        cur = con.execute(
            """INSERT INTO facts(doc_id, chunk_id, entity, attribute, value_text, value_num, unit,
                                 period, scope, qualifiers, statement, quote, page, page_label,
                                 grounded, confidence, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc_id, chunk_id, f["entity"], f["attribute"], f["value_text"], f.get("value_num"),
             f.get("unit"), f.get("period"), f.get("scope"),
             json.dumps(f.get("qualifiers") or {}), f["statement"], f["quote"], f["page"],
             f.get("page_label"), 1 if f.get("grounded") else 0, f.get("confidence"), time.time()),
        )
        return cur.lastrowid


def add_relation(a_id, b_id, kind, confidence, reasoning, resolution, cross_doc):
    a, b = (a_id, b_id) if a_id < b_id else (b_id, a_id)
    with db() as con:
        con.execute(
            """INSERT OR IGNORE INTO
               relations(a_id,b_id,kind,confidence,reasoning,resolution,cross_doc,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (a, b, kind, confidence, reasoning, resolution, 1 if cross_doc else 0, time.time()),
        )


def add_issue(doc_id, page, kind, detail, payload=None):
    with db() as con:
        con.execute(
            "INSERT INTO issues(doc_id,page,kind,detail,payload,created_at) VALUES (?,?,?,?,?,?)",
            (doc_id, page, kind, detail,
             json.dumps(payload) if payload is not None else None, time.time()),
        )
