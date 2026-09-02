"""
Betting sessions — the daily rhythm.

All times EAT (East Africa Time, UTC+3).  GitHub cron fires in UTC:
    MORNING   09:00 EAT -> 06:00 UTC
    AFTERNOON 14:00 EAT -> 11:00 UTC
    EVENING   19:00 EAT -> 16:00 UTC
    MIDNIGHT  23:30 EAT -> 20:30 UTC
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Session:
    """One daily betting session."""

    name: str
    emoji: str
    utc_hour: int
    utc_minute: int


SESSIONS: tuple[Session, ...] = (
    Session("MORNING", "🌅", 6, 0),
    Session("AFTERNOON", "☀️", 11, 0),
    Session("EVENING", "🌆", 16, 0),
    Session("MIDNIGHT", "🌙", 20, 30),
)


def detect(now: datetime | None = None) -> Session:
    """The most recent session whose start time has passed (cyclic)."""
    now = now or datetime.now(timezone.utc)
    now_minutes = now.hour * 60 + now.minute
    current = SESSIONS[-1]
    for session in SESSIONS:
        start = session.utc_hour * 60 + session.utc_minute
        if now_minutes >= start:
            current = session
    return current


def by_name(name: str) -> Session | None:
    """Look up a session by (case-insensitive) name; None if unknown."""
    wanted = name.strip().upper()
    for session in SESSIONS:
        if session.name == wanted:
            return session
    return None