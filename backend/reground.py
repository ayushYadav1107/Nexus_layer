"""Re-run the grounding check over facts already in the database.

Grounding is pure string comparison against a chunk that is already stored, so
when the rule changes there is no reason to pay for extraction again. This
re-evaluates every fact, updates `grounded`, and clears the `ungrounded_quote`
issues that no longer apply.

    python reground.py            # report what would change
    python reground.py --apply    # write it

Relations are NOT recomputed: judging costs API calls. Facts that newly become
grounded are eligible for linking, so re-upload the document (which now resets
and reruns it) if you want their relations too.
"""
import sys

import env  # noqa: F401  loads .env before the reads below
import ingest
import store


def main(apply):
    with store.db() as con:
        rows = con.execute(
            "SELECT f.id, f.grounded, f.quote, f.attribute, c.text "
            "FROM facts f JOIN chunks c ON c.id = f.chunk_id").fetchall()

    now_ok, now_bad = [], []
    for r in rows:
        grounded = ingest.is_grounded(r["quote"], r["text"])
        if grounded and not r["grounded"]:
            now_ok.append(r)
        elif not grounded and r["grounded"]:
            now_bad.append(r)

    print(f"\n{len(rows)} facts checked")
    print(f"  {len(now_ok):4} would become grounded")
    print(f"  {len(now_bad):4} would become ungrounded")
    for r in now_ok[:5]:
        print(f"       + {r['attribute']}: {r['quote'][:60]!r}")

    if not apply:
        print("\nDry run. Re-run with --apply to write.\n")
        return 0

    with store.db() as con:
        for r in now_ok:
            con.execute("UPDATE facts SET grounded=1 WHERE id=?", (r["id"],))
        for r in now_bad:
            con.execute("UPDATE facts SET grounded=0 WHERE id=?", (r["id"],))
        # Drop the stale complaints; anything still ungrounded is re-reported by
        # the next run over that document.
        removed = con.execute(
            "DELETE FROM issues WHERE kind='ungrounded_quote'").rowcount
        for r in rows:
            if not ingest.is_grounded(r["quote"], r["text"]):
                con.execute(
                    "INSERT INTO issues(doc_id,page,kind,detail,payload,created_at) "
                    "SELECT doc_id, page, 'ungrounded_quote', ?, NULL, created_at "
                    "FROM facts WHERE id=?",
                    (f"quote not found verbatim: {r['attribute']} = {r['quote'][:80]}",
                     r["id"]))

    print(f"\napplied. cleared {removed} stale ungrounded_quote issues.")
    print("Re-upload a document if you want relations for the recovered facts.\n")
    return 0


if __name__ == "__main__":
    store.init()
    sys.exit(main("--apply" in sys.argv))
