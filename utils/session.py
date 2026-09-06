"""
Betting sessions - 6 per day, 4 hours apart, RETIMED to global game supply.

    S1 09:00 | S2 13:00 | S3 17:00 | S4 21:00 | S5 01:00 | S6 05:00  (EAT)

The grid sits on top of when games actually START worldwide:
    S1/S2  Asia (KBO, NPB, J-League)   S3/S4  Europe prime
    S5     MLB night + Brazil          S6     MLB West Coast + NPB morning
Detection uses UTC so every machine agrees.
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
    Session("S1", "\U0001F305", 9),
    Session("S2", "\u2600", 13),
    Session("S3", "\U0001F305", 17),
    Session("S4", "\U0001F31F", 21),
    Session("S5", "\U0001F319", 1),
    Session("S6", "\u26A1", 5),
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
    """Hours until the next session starts."""
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