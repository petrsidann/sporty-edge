"""
Betting sessions - 4 per day at the supply-fat windows (EAT).

    S1 09:00 | S2 13:00 | S3 17:00 | S4 21:00

The 05:00 window (MLB West Coast only) is dropped - it was the chronic
shortfall.  Rolling 4h windows; every pick settles inside its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

EAT = timezone(timedelta(hours=3))


@dataclass(frozen=True)
class Session:
    name: str
    emoji: str
    eat_hour: int


SESSIONS: tuple[Session, ...] = (
    Session("S1", "\U0001F305", 9),
    Session("S2", "\u2600", 13),
    Session("S3", "\U0001F305", 17),
    Session("S4", "\U0001F31F", 21),
)


def detect(now: datetime | None = None) -> Session:
    """Session whose 6-hour block contains now (4 sessions x 6h)."""
    now = now or datetime.now(timezone.utc)
    h = now.astimezone(EAT).hour
    blocks = [9, 13, 17, 21]  # S1..S4 start hours
    idx = 3
    for i, start in enumerate(blocks):
        nxt = blocks[(i + 1) % 4]
        if nxt > start:
            if start <= h < nxt:
                idx = i
                break
        else:  # wraps past midnight (21 -> 9)
            if h >= start or h < nxt:
                idx = i
                break
    return SESSIONS[idx]


def next_start_eat(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    eat = now.astimezone(EAT)
    best = None
    for s in SESSIONS:
        start = eat.replace(hour=s.eat_hour % 24, minute=0, second=0, microsecond=0)
        if start <= eat:
            start += timedelta(hours=24)
        if best is None or start < best:
            best = start
    return best


def session_window(now: datetime | None = None) -> float:
    delta = (next_start_eat(now) - (now or datetime.now(timezone.utc))
             ).total_seconds() / 3600.0
    return max(0.5, delta)


def clock_line(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone()
    eat = now.astimezone(EAT)
    s = detect(now)
    nxt = next_start_eat(now)
    return (f"Local {local.strftime('%a %H:%M %Z')} | "
            f"EAT {eat.strftime('%H:%M')} | "
            f"session {s.emoji} {s.name} | next {nxt.strftime('%H:%M EAT')}")