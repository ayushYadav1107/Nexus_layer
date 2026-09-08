"""One structured-JSON call, against whichever model each role is pointed at.

The workload splits cleanly, so the models do too:

  extraction  653 calls of mostly transcription and normalisation. A local 8B
              model handles it, costs nothing, and can be re-run freely while
              tuning the prompt.
  judging     far fewer calls, but this is where corroborates / contradicts /
              reconciled is actually decided. Worth a hosted model.

Configured as "provider:model" per role:

  FACTLAYER_EXTRACT=ollama:llama3.1:8b
  FACTLAYER_JUDGE=gemini:gemini-3.8-flash

Either role also accepts anthropic:claude-sonnet-5. Providers are three small
functions and a dict -- there is no plugin layer, and adding a fourth is a
function plus one dict entry.

HTTP is stdlib urllib on a worker thread rather than an async HTTP dependency:
these are plain JSON POSTs, and _gate already bounds concurrency. Gemini's free
tier is the reason the gate also enforces requests-per-minute -- without it you
spend the run collecting 429s.
"""
import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager

EXTRACT_SPEC = os.environ.get("FACTLAYER_EXTRACT", "ollama:llama3.1:8b")
JUDGE_SPEC = os.environ.get("FACTLAYER_JUDGE", "gemini:gemini-3.8-flash")

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_TIMEOUT = int(os.environ.get("FACTLAYER_OLLAMA_TIMEOUT", "900"))
OLLAMA_CTX = int(os.environ.get("FACTLAYER_OLLAMA_CTX", "8192"))
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"


class LLMError(RuntimeError):
    pass


def _spec(raw):
    """'gemini:gemini-3.8-flash' -> ('gemini', 'gemini-3.8-flash').
    Model names may contain ':' (ollama tags), so split once only."""
    provider, _, model = raw.partition(":")
    if not model:
        raise LLMError(f"bad model spec {raw!r}; expected 'provider:model'")
    return provider.strip().lower(), model.strip()


# Per role: bounded concurrency, plus a minimum interval between starts so a
# free-tier requests-per-minute cap is respected rather than discovered.
def _make_gate(concurrency, rpm):
    return {"sem": asyncio.Semaphore(concurrency), "lock": asyncio.Lock(),
            "interval": 60.0 / rpm if rpm else 0.0, "next": 0.0}


_GATES = {
    "extract": _make_gate(int(os.environ.get("FACTLAYER_EXTRACT_CONCURRENCY", "2")),
                          int(os.environ.get("FACTLAYER_EXTRACT_RPM", "0"))),
    "judge": _make_gate(int(os.environ.get("FACTLAYER_JUDGE_CONCURRENCY", "2")),
                        int(os.environ.get("FACTLAYER_JUDGE_RPM", "14"))),
}
_SPECS = {"extract": _spec(EXTRACT_SPEC), "judge": _spec(JUDGE_SPEC)}


@asynccontextmanager
async def _gate(g):
    async with g["sem"]:
        if g["interval"]:
            async with g["lock"]:
                wait = g["next"] - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                g["next"] = time.monotonic() + g["interval"]
        yield


def _post_sync(url, payload, headers, timeout):
    """POST JSON, retrying rate limits and transient failures. Runs on a thread."""
    body = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json", **headers}
    last = None
    for attempt in range(5):
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last = f"HTTP {e.code}: {detail}"
            if e.code not in (408, 409, 429, 500, 502, 503, 504) or attempt == 4:
                raise LLMError(last)
            # Honour Retry-After when the server sends one; otherwise back off.
            try:
                delay = float(e.headers.get("Retry-After") or 0)
            except ValueError:
                delay = 0.0
            time.sleep(min(delay or 2 ** attempt, 60))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = f"{type(e).__name__}: {e}"
            if attempt == 4:
                raise LLMError(last)
            time.sleep(2 ** attempt)
        except json.JSONDecodeError as e:
            raise LLMError(f"non-JSON response: {e}")
    raise LLMError(last or "request failed")


# ---------------------------------------------------------------- providers

async def _ollama(model, system, user, schema, max_tokens):
    # num_ctx is the whole window, prompt included -- Ollama defaults it to 4096,
    # which silently leaves almost no room for output once the system prompt and a
    # 4000-character chunk are in. Raising it costs VRAM, so on a small GPU this is
    # the knob that decides whether the model still fits (check `ollama ps`: any
    # "CPU/GPU" split means layers spilled to CPU and generation is several times
    # slower).
    data = await asyncio.to_thread(
        _post_sync, f"{OLLAMA_HOST}/api/chat",
        {"model": model,
         "messages": [{"role": "system", "content": system},
                      {"role": "user", "content": user}],
         "format": schema,          # Ollama constrains decoding to the JSON schema
         "stream": False,
         "options": {"temperature": 0,
                     "num_ctx": OLLAMA_CTX,
                     "num_predict": min(max_tokens, OLLAMA_CTX // 2)}},
        {}, OLLAMA_TIMEOUT)
    text = (data.get("message") or {}).get("content")
    if not text:
        raise LLMError(f"empty ollama response: {json.dumps(data)[:200]}")
    return text


def _gemini_schema(s):
    """generateContent takes an OpenAPI-flavoured subset of JSON Schema: no
    additionalProperties, and nullability as a flag rather than a type union."""
    if not isinstance(s, dict):
        return s
    out = {}
    for k, v in s.items():
        if k == "additionalProperties":
            continue
        if k == "type" and isinstance(v, list):
            concrete = [t for t in v if t != "null"]
            out["type"] = concrete[0] if concrete else "string"
            if "null" in v:
                out["nullable"] = True
        elif k == "properties":
            out[k] = {pk: _gemini_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out[k] = _gemini_schema(v)
        else:
            out[k] = v
    return out


async def _gemini(model, system, user, schema, max_tokens):
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise LLMError("GEMINI_API_KEY is not set")
    data = await asyncio.to_thread(
        _post_sync, f"{GEMINI_ENDPOINT}/{model}:generateContent",
        {"systemInstruction": {"parts": [{"text": system}]},
         "contents": [{"role": "user", "parts": [{"text": user}]}],
         "generationConfig": {"responseMimeType": "application/json",
                              "responseSchema": _gemini_schema(schema),
                              "maxOutputTokens": max_tokens,
                              "temperature": 0}},
        {"x-goog-api-key": key}, 180)

    blocked = (data.get("promptFeedback") or {}).get("blockReason")
    if blocked:
        raise LLMError(f"prompt blocked: {blocked}")
    candidates = data.get("candidates") or []
    if not candidates:
        raise LLMError(f"no candidates: {json.dumps(data)[:300]}")
    reason = candidates[0].get("finishReason")
    if reason not in (None, "STOP"):
        # MAX_TOKENS here means truncated JSON, which would fail to parse anyway.
        raise LLMError(f"finishReason={reason}")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    if not text:
        raise LLMError("empty gemini response")
    return text


async def _anthropic(model, system, user, schema, max_tokens, effort):
    import anthropic  # optional: only needed if a role points at anthropic:

    global _anthropic_client
    try:
        _anthropic_client
    except NameError:
        _anthropic_client = None
    if _anthropic_client is None:
        try:
            _anthropic_client = anthropic.AsyncAnthropic(max_retries=4)
        except Exception as e:
            raise LLMError(f"no Anthropic credentials: {e}") from e

    try:
        resp = await _anthropic_client.messages.create(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema},
                           "effort": effort})
    except Exception as e:
        raise LLMError(f"{type(e).__name__}: {str(e)[:300]}") from e
    if resp.stop_reason == "refusal":
        raise LLMError(f"refused: {getattr(resp, 'stop_details', None)}")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise LLMError(f"no text block (stop_reason={resp.stop_reason})")
    return text


# ---------------------------------------------------------------- entry point

async def structured(system, user, schema, role="extract", effort="medium",
                     max_tokens=8000):
    """One JSON-schema-constrained call for `role`. Raises LLMError on failure."""
    provider, model = _SPECS[role]
    async with _gate(_GATES[role]):
        if provider == "ollama":
            text = await _ollama(model, system, user, schema, max_tokens)
        elif provider == "gemini":
            text = await _gemini(model, system, user, schema, max_tokens)
        elif provider == "anthropic":
            text = await _anthropic(model, system, user, schema, max_tokens, effort)
        else:
            raise LLMError(f"unknown provider {provider!r} in {role} spec")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"bad json from {provider}: {e}; head={text[:200]!r}") from e


def describe():
    """Which model each role resolves to -- surfaced by GET /stats."""
    return {role: f"{p}:{m}" for role, (p, m) in _SPECS.items()}
