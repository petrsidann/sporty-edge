"""
Betting sessions - the daily rhythm, with honest clocks.

Boundaries (EAT):  MORNING 06:00-13:00 | AFTERNOON 13:00-18:00
                   EVENING 18:00-23:00 | MIDNIGHT 23:00-06:00
Detection uses UTC so every machine (laptop, GitHub Actions) agrees.
clock_line() shows Local + EAT + UTC + the detected session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

EAT = timezone(timedelta(hours=3))


@dataclass(frozen=True)
class Session:
    """One daily betting session."""

    name: str
    emoji: str
    utc_hour: int
    utc_minute: int


SESSIONS: tuple[Session, ...] = (
    Session("MORNING", "\U0001F305", 3, 0),     # 06:00 EAT
    Session("AFTERNOON", "\u2600", 10, 0),      # 13:00 EAT
    Session("EVENING", "\U0001F306", 15, 0),    # 18:00 EAT
    Session("MIDNIGHT", "\U0001F319", 20, 0),   # 23:00 EAT
)


def detect(now: datetime | None = None) -> Session:
    """Session in progress now (wraps correctly past midnight)."""
    now = now or datetime.now(timezone.utc)
    now_min = now.hour * 60 + now.minute
    current = SESSIONS[-1]
    for session in SESSIONS:
        if now_min >= session.utc_hour * 60 + session.utc_minute:
            current = session
    return current


def by_name(name: str) -> Session | None:
    """Look up a session by (case-insensitive) name; None if unknown."""
    wanted = name.strip().upper()
    for session in SESSIONS:
        if session.name == wanted:
            return session
    return None


def clock_line(now: datetime | None = None) -> str:
    """Dual-clock status: your Local time + EAT + UTC + session."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone()
    eat = now.astimezone(EAT)
    session = detect(now)
    return (
        f"Local {local.strftime('%a %H:%M %Z')} | "
        f"EAT {eat.strftime('%H:%M')} | "
        f"UTC {now.strftime('%H:%M')} | "
        f"session {session.emoji} {session.name}"
    )