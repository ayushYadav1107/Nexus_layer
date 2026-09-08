"""Thin async wrapper around the Anthropic Messages API.

Everything the pipeline asks the model for is a JSON object matching a schema,
so there is exactly one call shape. The stable half of each prompt lives in
`system` and is cache-marked; the volatile half (the chunk, the fact pair) goes
in the user turn, which is the ordering prompt caching needs.
"""
import asyncio
import json
import os

import anthropic

MODEL = os.environ.get("FACTLAYER_MODEL", "claude-opus-5")
CONCURRENCY = int(os.environ.get("FACTLAYER_CONCURRENCY", "8"))

_client = None
_sem = asyncio.Semaphore(CONCURRENCY)


class LLMError(RuntimeError):
    pass


def client():
    """Built on first use, not at import -- the server must still start and serve
    already-extracted results when no API key is configured."""
    global _client
    if _client is None:
        try:
            _client = anthropic.AsyncAnthropic(max_retries=4)
        except Exception as e:
            raise LLMError(f"no Anthropic credentials: set ANTHROPIC_API_KEY ({e})") from e
    return _client


async def structured(system, user, schema, effort="medium", max_tokens=8000):
    """One JSON-schema-constrained call. Raises LLMError on failure."""
    req = dict(
        model=MODEL,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema}, "effort": effort},
    )
    async with _sem:
        try:
            resp = await client().messages.create(**req)
        except anthropic.BadRequestError as e:
            if "effort" not in str(e):
                raise LLMError(str(e)[:400]) from e
            # Older endpoints reject `effort` alongside `format`; the schema matters more.
            req["output_config"] = {"format": req["output_config"]["format"]}
            try:
                resp = await client().messages.create(**req)
            except anthropic.APIError as e2:
                raise LLMError(str(e2)[:400]) from e2
        except anthropic.APIError as e:
            raise LLMError(str(e)[:400]) from e
        except Exception as e:
            # Credential resolution, DNS, TLS, a bad proxy: anything that is not an
            # APIError still has to arrive as an LLMError, so one broken chunk is
            # recorded as an issue instead of taking the whole document down.
            raise LLMError(f"{type(e).__name__}: {str(e)[:300]}") from e

    if resp.stop_reason == "refusal":
        raise LLMError(f"refused: {getattr(resp, 'stop_details', None)}")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise LLMError(f"no text block (stop_reason={resp.stop_reason})")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"bad json: {e}; head={text[:200]!r}") from e
