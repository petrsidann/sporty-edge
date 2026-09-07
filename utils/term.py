"""
Console hardening for the Windows Task Scheduler.

The scheduler runs this repo under a cp1252 console; printing a session
emoji (e.g. S4 '\U0001F31F' from utils/session.py) raised UnicodeEncodeError
and killed the whole run (see data/scheduler.log).  Calling force_utf8_stdio()
at the top of every scheduler-facing entry point makes output survive any
console code page: UTF-8 where supported, replacement characters elsewhere.
"""

from __future__ import annotations

import sys


def force_utf8_stdio() -> None:
    """Reconfigure stdout/stderr to UTF-8 with replacement on error.

    Never raises: on streams without reconfigure (pytest capture, pipes that
    are already UTF-8) this is a harmless no-op.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError, AttributeError):
                pass
