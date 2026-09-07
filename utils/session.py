"""Betting sessions - owner spec (EAT):
    S1 07:00-10:00  US late sports, A-League, J-League      target 10
    S2 12:00-15:00  E.Europe, tennis, Asian zone             target 10
    S3 17:00-23:00  FAT: EPL/UCL/LaLiga/SerieA/Euroleague    target 25
    S4 00:00-03:00  South America, NFL/NBA/NCAA early        target 10
During gaps the system pre-loads the NEXT session's board (window starts now,
ends at that session's end) - so picks are always settle-before-rebet."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

EAT = timezone(timedelta(hours=3))


@dataclass(frozen=True)
class Session:
    name: str
    emoji: str
    start_hour: int
    end_hour: int          # wraps past 24 for S4
    target: int


SESSIONS = (
    Session("S1", "\U0001F305", 7, 10, 10),
    Session("S2", "\u2600", 12, 15, 10),
    Session("S3", "\U0001F31F", 17, 23, 25),
    Session("S4", "\U0001F319", 0, 3, 10),
)


def _contains(s: Session, h: int) -> bool:
    if s.start_hour < s.end_hour:
        return s.start_hour <= h < s.end_hour
    return h >= s.start_hour or h < s.end_hour


def _session_start_dt(s: Session, now_eat: datetime) -> datetime:
    st = now_eat.replace(hour=s.start_hour % 24, minute=0, second=0,
                         microsecond=0)
    if st > now_eat:
        st -= timedelta(hours=24)   # S4 already running past midnight
    return st


def _session_end_dt(s: Session, now_eat: datetime) -> datetime:
    en = now_eat.replace(hour=s.end_hour % 24, minute=0, second=0,
                         microsecond=0)
    if en <= now_eat and s.end_hour <= s.start_hour:
        en += timedelta(hours=24)
    elif en < now_eat:
        en += timedelta(hours=24)
    return en


def current_or_next(now: datetime | None = None) -> tuple[Session, datetime, datetime]:
    """(session, window_start, window_end). In a gap: the NEXT session,
    window starting now (pre-load) and ending at that session's end."""
    now = now or datetime.now(timezone.utc)
    eat = now.astimezone(EAT)
    for s in SESSIONS:
        if _contains(s, eat.hour):
            return s, max(now, _session_start_dt(s, eat).astimezone(timezone.utc)), \
                   _session_end_dt(s, eat).astimezone(timezone.utc)
    # gap -> next session to start
    best = None
    for s in SESSIONS:
        st = eat.replace(hour=s.start_hour % 24, minute=0, second=0, microsecond=0)
        if st <= eat:
            st += timedelta(hours=24)
        if best is None or st < best[0]:
            best = (st, s)
    st, s = best
    return s, now, _session_end_dt(s, st.astimezone(EAT)).astimezone(timezone.utc)


def detect(now: datetime | None = None) -> Session:
    return current_or_next(now)[0]


def clock_line(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    s, ws, we = current_or_next(now)
    return (f"Local {now.astimezone().strftime('%a %H:%M %Z')} | "
            f"EAT {now.astimezone(EAT).strftime('%H:%M')} | "
            f"session {s.emoji} {s.name} (target {s.target}) | "
            f"window ends {we.astimezone(EAT).strftime('%H:%M EAT')}")