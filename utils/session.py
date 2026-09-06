"""
Betting sessions - 6 per day, 4 hours apart (EAT).

    S1 08:00 | S2 12:00 | S3 16:00 | S4 20:00 | S5 00:00 | S6 04:00

v3 fix: detect() and next_start_eat() now map blocks to the CORRECT
session (07:09 is inside S6's block, not S2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

EAT = timezone(timedelta(hours=3))
GAME_HOURS = 2.5


@dataclass(frozen=True)
class Session:
    """One daily betting session."""

    name: str
    emoji: str
    eat_hour: int


SESSIONS: tuple[Session, ...] = (
    Session("S1", "\U0001F305", 8),
    Session("S2", "\u2600", 12),
    Session("S3", "\U0001F305", 16),
    Session("S4", "\U0001F31F", 20),
    Session("S5", "\U0001F319", 0),
    Session("S6", "\u26A1", 4),
)


def detect(now: datetime | None = None) -> Session:
    """The session whose [start, start+4h) block contains now."""
    now = now or datetime.now(timezone.utc)
    h = now.astimezone(EAT).hour
    for s in SESSIONS:
        start, end = s.eat_hour, (s.eat_hour + 4) % 24
        if end > start:
            if start <= h < end:
                return s
        elif h >= start or h < end:
            return s
    return SESSIONS[0]


def next_start_eat(now: datetime | None = None) -> datetime:
    """EAT datetime of the next session start strictly after now."""
    now = now or datetime.now(timezone.utc)
    eat = now.astimezone(EAT)
    best = None
    for s in SESSIONS:
        start = eat.replace(hour=s.eat_hour % 24, minute=0, second=0,
                            microsecond=0)
        if start <= eat:
            start += timedelta(hours=24)
        if best is None or start < best:
            best = start
    return best


def session_window(now: datetime | None = None) -> float:
    """Hours until the next session starts (kickoff bound = this - game)."""
    delta = (next_start_eat(now) - (now or datetime.now(timezone.utc))
             ).total_seconds() / 3600.0
    return max(0.25, delta)


def clock_line(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone()
    eat = now.astimezone(EAT)
    session = detect(now)
    nxt = next_start_eat(now)
    return (
        f"Local {local.strftime('%a %H:%M %Z')} | "
        f"EAT {eat.strftime('%H:%M')} | "
        f"session {session.emoji} {session.name} | "
        f"next {nxt.strftime('%H:%M EAT')}"
    )