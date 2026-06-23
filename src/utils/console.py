"""Console output helpers.

On Windows, Python's default stdout encoding is often `cp1252`, which
crashes when printing common Unicode characters like bullets or emoji
that LLMs love to include in answers. We reconfigure stdout/stderr to
UTF-8 at entrypoints so output is robust.
"""

from __future__ import annotations

import sys


def ensure_utf8_stdout() -> None:
    """Reconfigure stdout/stderr to UTF-8 if the runtime allows it.

    Safe to call multiple times. Falls back silently on Python builds that
    do not expose `reconfigure` on the standard streams.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
