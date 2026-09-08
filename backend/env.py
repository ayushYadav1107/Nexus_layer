"""Load `backend/.env` into os.environ, once, before anything reads it.

Shell variables die with the terminal window, which meant re-exporting a key and
two model settings every time a new terminal was opened -- and silently running
with the wrong configuration when that was forgotten.

A real environment variable always wins over the file, so `FACTLAYER_DB=... python
foo.py` and CI both still override it, and the test suite's explicit settings are
never clobbered. The format is KEY=value with # comments and optional surrounding
quotes; that is a dozen lines of stdlib, so no dependency is worth adding for it.

Import this before any module that reads os.environ at import time.
"""
import os
from pathlib import Path

PATH = Path(__file__).with_name(".env")


def load(path=PATH):
    """Returns the names it set. Missing file is not an error -- everything has a
    default, and the shell may be supplying the values instead."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    applied = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        # Strip one matched pair of quotes; an API key can legitimately contain
        # almost anything else, so nothing further is interpreted -- in particular
        # a '#' inside a value is part of the value, not a comment.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            applied.append(key)
    return applied


load()
