"""
Betting sessions - 6 per day, exactly 4 hours apart (EAT).

    S1 08:00 | S2 12:00 | S3 16:00 | S4 20:00 | S5 00:00 | S6 04:00

Detection uses UTC so every machine agrees.  Every session has a HARD
boundary: the system may only pick games that FINISH (kickoff + ~2.5h)
before the next session starts - so matches never bleed across sessions.
clock_line() shows Local + EAT + UTC + session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

EAT = timezone(timedelta(hours=3))
GAME_HOURS = 2.5          # average game duration used for boundaries


@dataclass(frozen=True)
class Session:
    """One daily betting session."""

    name: str
    emoji: str
    eat_hour: int          # session start in EAT


SESSIONS: tuple[Session, ...] = (
    Session("S1", "\U0001F305", 8),
    Session("S2", "\u2600", 12),
    Session("S3", "\U0001F305", 16),
    Session("S4", "\U0001F31F", 20),
    Session("S5", "\U0001F319", 0),
    Session("S6", "\u26A1", 4),
)


def detect(now: datetime | None = None) -> Session:
    """The session currently in progress (4h blocks, wrapping at midnight)."""
    now = now or datetime.now(timezone.utc)
    eat = now.astimezone(EAT)
    block = (eat.hour // 4) % 6
    return SESSIONS[block]


def by_name(name: str) -> Session | None:
    """Accept 's1'..'s6' or 'S1'..'S6'; None if unknown."""
    wanted = name.strip().upper()
    for session in SESSIONS:
        if session.name == wanted:
            return session
    return None


def next_start_eat(now: datetime | None = None) -> datetime:
    """EAT datetime of the next session start after ``now``."""
    now = now or datetime.now(timezone.utc)
    eat = now.astimezone(EAT)
    block = (eat.hour // 4) % 6
    this = SESSIONS[block]
    start = eat.replace(hour=this.eat_hour % 24, minute=0, second=0,
                        microsecond=0)
    if start > eat:
        return start
    return start + timedelta(hours=4)


def session_window(now: datetime | None = None) -> float:
    """Max hours ahead a game may KICK OFF so it finishes before the next
    session: (hours until next session) - game length.  Floor 0.5h."""
    delta = (next_start_eat(now) - (now or datetime.now(timezone.utc))
             ).total_seconds() / 3600.0
    return max(0.5, delta - GAME_HOURS)


def clock_line(now: datetime | None = None) -> str:
    """Dual-clock status: your Local time + EAT + UTC + session + window."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone()
    eat = now.astimezone(EAT)
    session = detect(now)
    nxt = next_start_eat(now)
    window = session_window(now)
    return (
        f"Local {local.strftime('%a %H:%M %Z')} | "
        f"EAT {eat.strftime('%H:%M')} | "
        f"session {session.emoji} {session.name} | "
        f"next {nxt.strftime('%H:%M EAT')} | "
        f"games must start within {window:.1f}h"
    )