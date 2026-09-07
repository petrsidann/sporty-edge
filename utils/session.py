"""Betting sessions - 4/day (EAT): S1 09:00, S2 13:00, S3 17:00, S4 21:00."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

EAT = timezone(timedelta(hours=3))


@dataclass(frozen=True)
class Session:
    name: str
    emoji: str
    eat_hour: int


SESSIONS = (
    Session("S1", "\U0001F305", 9),
    Session("S2", "\u2600", 13),
    Session("S3", "\U0001F305", 17),
    Session("S4", "\U0001F31F", 21),
)


def detect(now: datetime | None = None) -> Session:
    """Session whose 6-hour block contains now."""
    now = now or datetime.now(timezone.utc)
    h = now.astimezone(EAT).hour
    for i, s in enumerate(SESSIONS):
        start = s.eat_hour
        nxt = SESSIONS[(i + 1) % len(SESSIONS)].eat_hour
        if nxt > start:
            if start <= h < nxt:
                return s
        else:  # S4 wraps past midnight (21 -> 9)
            if h >= start or h < nxt:
                return s
    return SESSIONS[0]


def next_start_eat(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    eat = now.astimezone(EAT)
    best = None
    for s in SESSIONS:
        st = eat.replace(hour=s.eat_hour % 24, minute=0,
                         second=0, microsecond=0)
        if st <= eat:
            st += timedelta(hours=24)
        if best is None or st < best:
            best = st
    return best


def session_window(now: datetime | None = None) -> float:
    return max(0.5, (next_start_eat(now)
                     - (now or datetime.now(timezone.utc))).total_seconds() / 3600.0)


def clock_line(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    eat = now.astimezone(EAT)
    s = detect(now)
    return (f"Local {now.astimezone().strftime('%a %H:%M %Z')} | "
            f"EAT {eat.strftime('%H:%M')} | session {s.emoji} {s.name} | "
            f"next {next_start_eat(now).strftime('%H:%M EAT')}")