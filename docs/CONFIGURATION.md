# Configuration

All settings come from `backend/.env`, loaded by `env.py` before anything reads
`os.environ`. A real environment variable always wins over the file, so CI and one-off
overrides still work — and the test suite is never affected by a stray `.env`.

```bash
cd backend
cp .env.example .env
```

`.env` is gitignored. `.env.example` is the committed template.

> Shell variables die with the terminal window. Re-exporting a key and two model settings
> on every new terminal is how one run silently spent 47 minutes producing nothing — the
> file exists so that cannot happen.

---

## The two roles

The workload splits cleanly, so the models do:

| Role | Calls | What it does | Wants |
| --- | --- | --- | --- |
| **extract** | one per chunk — hundreds | Mostly transcription and normalisation | Cheap and fast |
| **judge** | one per grounded fact | Decides corroborates / contradicts / reconciled | Stronger reasoning |

Each is set independently as `provider:model`:

```
FACTLAYER_EXTRACT=gemini:gemini-3.5-flash-lite
FACTLAYER_JUDGE=gemini:gemini-2.5-flash
```

Providers: `gemini`, `ollama`, `anthropic`. Mix freely — the point of the split is that
neither role is pinned.

---

## Choosing a model

Run the preflight first. One real call per role, five seconds:

```bash
python check_models.py
```

**Being listed does not mean you have quota for it.** `gemini-3.8-flash` appears in the
models endpoint and 429s on every call with a free key. Verified working on a free key:

| Model | Latency |
| --- | ---: |
| `gemini-3.5-flash-lite` | 2.4 s |
| `gemini-flash-lite-latest` | 3.7 s |
| `gemini-2.5-flash` | 5.5 s |
| `gemini-3.1-flash-lite` | 6.3 s |

To see what a key can reach:

```bash
python check_models.py --list
```

A 429 on the first call means no quota for that model; a 400 means the key itself is
wrong. The preflight names the likely cause per status code.

---

## All variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` | — | Required if either role is `gemini:` |
| `ANTHROPIC_API_KEY` | — | Required if either role is `anthropic:` |
| `FACTLAYER_EXTRACT` | `gemini:gemini-3.5-flash-lite` | Extraction model |
| `FACTLAYER_JUDGE` | `gemini:gemini-2.5-flash` | Judging model |
| `FACTLAYER_EXTRACT_RPM` | `0` (off) | Requests/min ceiling for extraction |
| `FACTLAYER_JUDGE_RPM` | `14` | Requests/min ceiling for judging |
| `FACTLAYER_EXTRACT_CONCURRENCY` | `2` | Parallel extraction calls |
| `FACTLAYER_JUDGE_CONCURRENCY` | `2` | Parallel judging calls |
| `FACTLAYER_CHUNK_CHARS` | `4000` | Chars per chunk; 8000 means ~30% fewer calls |
| `FACTLAYER_DB` | `backend/factlayer.db` | SQLite file; delete to reset the layer |
| `FACTLAYER_MAX_UPLOAD_MB` | `50` | Upload size cap |
| `OLLAMA_HOST` | `http://localhost:11434` | Only used when a role is `ollama:` |
| `FACTLAYER_OLLAMA_CTX` | `8192` | Ollama context window — see below |

---

## Cost and pacing

Everything runs on free tiers with the defaults. **The binding constraint is time, not
money.** Run time is almost entirely the rate gate: chunks ÷ RPM.

Check your real limits at <https://aistudio.google.com/rate-limit> and raise `*_RPM` to
match. Setting it **above** your actual quota makes runs slower, not faster — 429s trigger
backoff.

Two levers, in order of effect:

1. **Raise RPM to your real limit**, and raise concurrency with it (roughly `RPM ÷ 6`) or
   the gate just idles.
2. **`FACTLAYER_CHUNK_CHARS=8000`** — measured on the Delhivery set, 362 chunks → 251, so
   about 30% fewer extraction calls. Diminishing after 8000, since most pages already fit.

Judging cannot be sped up structurally: it is already one batched call per fact covering
all its candidates.

If you point both roles at Anthropic instead, the full six-document corpus is roughly $80
on `claude-opus-5`, $32 on `claude-sonnet-5`, $12 on `claude-haiku-4-5` — estimates from
token counts, not a measured bill.

**You almost certainly should not run all six documents.** The four required cases are
best shown with the **Q4 FY24 earnings deck plus the 2022 prospectus**: two years apart,
so period-based reconciliation and changed-director contradictions arise naturally, and
cross-document links are guaranteed.

---

## Running extraction locally

Set `FACTLAYER_EXTRACT=ollama:llama3.1:8b` for free, unlimited, offline extraction.
Ollama's `format` parameter takes a JSON schema, so the structured-output contract holds.

Two things to know, measured on a 6 GB RTX 4050:

- **`ollama ps` reports `32%/68% CPU/GPU`** at 8k context — the model does not fit, so a
  third of the layers run on CPU. That is **~117 s per chunk**, making a 148-chunk pair a
  3–5 hour job.
- **Quality is the real ceiling.** An 8B model fills the schema perfectly and follows the
  instructions poorly: it left `period` null on 30% of facts and `scope` null on most,
  and those are exactly the fields the judge needs to tell a contradiction from a
  reporting difference.

`FACTLAYER_OLLAMA_CTX` is the whole window, prompt included. Ollama's 4096 default leaves
almost no room for output once a system prompt and a 4000-character chunk are in; raising
it costs VRAM.

Local extraction is best used for iterating on prompts without cost anxiety, then
switching back to a hosted model for the run that counts.
