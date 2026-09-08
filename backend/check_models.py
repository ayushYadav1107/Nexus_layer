"""Preflight: make one real call per role and report what came back.

Run this before ingesting anything. Both roles talk to something external -- a
local Ollama server and a hosted API -- and a wrong model name or a missing key
should cost five seconds here, not an hour of extraction that ends in 653
identical llm_error issues.

    python check_models.py
"""
import asyncio
import sys

import env  # noqa: F401  loads .env before the reads below

import llm

SCHEMA = {
    "type": "object",
    "properties": {
        "capital": {"type": "string"},
        "population_millions": {"type": ["number", "null"]},
    },
    "required": ["capital", "population_millions"],
    "additionalProperties": False,
}
SYSTEM = "You answer with a JSON object matching the schema. Be accurate and terse."
USER = "What is the capital of France, and its population in millions?"


async def check(role):
    spec = "%s:%s" % llm._SPECS[role]
    print(f"  {role:8} -> {spec}")
    try:
        out = await llm.structured(SYSTEM, USER, SCHEMA, role=role, max_tokens=512)
    except llm.LLMError as e:
        print(f"  {'':8}    FAIL  {e}\n")
        return False
    missing = [k for k in SCHEMA["required"] if k not in out]
    if missing:
        print(f"  {'':8}    FAIL  schema not honoured, missing {missing}: {out}\n")
        return False
    print(f"  {'':8}    ok    {out}\n")
    return True


def list_gemini():
    """Which models this key can actually reach. A 429 on a model that simply has
    no free-tier quota looks identical to a genuine rate limit, so being able to
    see the list is the difference between a fix and an afternoon of guessing."""
    import json
    import os
    import urllib.request
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        print("GEMINI_API_KEY is not set; nothing to list.")
        return 1
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
        headers={"x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            models = json.loads(r.read().decode()).get("models", [])
    except Exception as e:
        print(f"could not list models: {type(e).__name__}: {e}")
        return 1
    usable = sorted(m["name"].removeprefix("models/") for m in models
                    if "generateContent" in (m.get("supportedGenerationMethods") or []))
    print(f"\n{len(usable)} models support generateContent with this key:\n")
    for name in usable:
        print(f"  {name}")
    print("\nPut one in backend/.env as FACTLAYER_EXTRACT / FACTLAYER_JUDGE, e.g.")
    print("  FACTLAYER_EXTRACT=gemini:gemini-2.5-flash\n")
    print("Being listed is not the same as having free-tier quota for it -- if a")
    print("model 429s immediately, try a smaller one (flash-lite, then 2.5-flash).\n")
    return 0


async def main():
    print("\nPreflight: one real call per role\n")
    results = [await check(role) for role in ("extract", "judge")]
    if all(results):
        print("Both roles are live. Safe to ingest.\n")
        return 0
    print(f"Settings come from {env.PATH} (shell variables override it).")
    print("Fix the failing role before ingesting. Common causes:")
    print("  ollama  - server not running (`ollama serve`), or model not pulled")
    print("            (`ollama pull llama3.1:8b`)")
    print("  gemini  - a 429 means the key works but has no quota for that model.")
    print("            See what it can reach:   python check_models.py --list")
    print("            Then try a smaller model, which usually has free quota:")
    print("            FACTLAYER_EXTRACT=gemini:gemini-2.5-flash-lite")
    print("          - a 400 means the key itself is wrong (check for a stray space)")
    print("          - blank key -> fill GEMINI_API_KEY in backend/.env\n")
    return 1


if __name__ == "__main__":
    if "--list" in sys.argv:
        sys.exit(list_gemini())
    sys.exit(asyncio.run(main()))
