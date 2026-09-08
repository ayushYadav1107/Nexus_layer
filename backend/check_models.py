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
    print("  gemini  - GEMINI_API_KEY blank in .env, or the model name is not one")
    print("            your key can reach; list what it can reach with:")
    print("            curl 'https://generativelanguage.googleapis.com/v1beta/models'"
          " -H \"x-goog-api-key: $GEMINI_API_KEY\"\n")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
