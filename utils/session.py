"""Betting sessions (owner spec). Roll-forward: when the current session
has <2h left, the scanner automatically serves the NEXT session's board
(window starts now) - so late-cycle runs always have real supply."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

EAT = timezone(timedelta(hours=3))
ROLL_AHEAD_HOURS = 2.0


@dataclass(frozen=True)
class Session:
    name: str
    emoji: str
    start_hour: int
    end_hour: int
    target: int


SESSIONS = (
    Session("S1", "\U0001F305", 7, 10, 5),
    Session("S2", "\u2600", 12, 15, 5),
    Session("S3", "\U0001F31F", 17, 23, 8),
    Session("S4", "\U0001F319", 0, 3, 5),
)


def _contains(s: Session, h: int) -> bool:
    if s.start_hour < s.end_hour:
        return s.start_hour <= h < s.end_hour
    return h >= s.start_hour or h < s.end_hour


def _start_dt(s: Session, eat: datetime) -> datetime:
    st = eat.replace(hour=s.start_hour % 24, minute=0, second=0, microsecond=0)
    if st > eat:
        st -= timedelta(hours=24)
    return st


def _end_dt(s: Session, eat: datetime) -> datetime:
    en = eat.replace(hour=s.end_hour % 24, minute=0, second=0, microsecond=0)
    if en <= eat:
        en += timedelta(hours=24)
    return en


def current_or_next(now: datetime | None = None) -> tuple[Session, datetime, datetime]:
    """(session, window_start, window_end).

    Inside a session with >=2h left -> that session.
    Inside a session with <2h left, or in a gap -> NEXT session,
    window starting NOW (pre-load) and ending at that session's end.
    """
    now = now or datetime.now(timezone.utc)
    eat = now.astimezone(EAT)

    for s in SESSIONS:
        if _contains(s, eat.hour):
            end_utc = _end_dt(s, eat).astimezone(timezone.utc)
            hours_left = (end_utc - now).total_seconds() / 3600.0
            if hours_left >= ROLL_AHEAD_HOURS:
                start_utc = max(now, _start_dt(s, eat).astimezone(timezone.utc))
                return s, start_utc, end_utc
            break  # nearly over -> roll to the next session

    best = None
    for s in SESSIONS:
        st = eat.replace(hour=s.start_hour % 24, minute=0, second=0, microsecond=0)
        if st <= eat:
            st += timedelta(hours=24)
        if best is None or st < best[0]:
            best = (st, s)
    st, s = best
    return s, now, _end_dt(s, st.astimezone(EAT)).astimezone(timezone.utc)


def detect(now: datetime | None = None) -> Session:
    return current_or_next(now)[0]


def clock_line(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    s, ws, we = current_or_next(now)
    return (f"Local {now.astimezone().strftime('%a %H:%M %Z')} | "
            f"EAT {now.astimezone(EAT).strftime('%H:%M')} | "
            f"session {s.emoji} {s.name} (target {s.target}) | "
            f"window ends {we.astimezone(EAT).strftime('%H:%M EAT')}")