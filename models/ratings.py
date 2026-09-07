"""models/ratings.py - recency-weighted win-rate ratings + log5, all sports."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

_DATA = Path("data")
HALF_LIFE = {"tennis": 180.0, "nba": 400.0, "nfl": 400.0, "mlb": 400.0}
HOME_EDGE = {"tennis": 0.0, "nba": 0.04, "nfl": 0.02, "mlb": 0.04}
_store: dict[str, dict[str, list[float]]] = {}
_loaded = False


def _weight(age_days: float, sport: str) -> float:
    return 0.5 ** (age_days / HALF_LIFE[sport])


def _norm(name: str) -> str:
    return " ".join((name or "").lower().split())


def _add_game(sport: str, winner: str, loser: str, date_iso: str) -> None:
    try:
        d = datetime.fromisoformat(date_iso).date()
    except (ValueError, TypeError):
        return
    age = (_today() - d).days
    if age < 0:
        return
    wgt = _weight(float(age), sport)
    st = _store.setdefault(sport, {})
    a = st.setdefault(_norm(winner), [0.0, 0.0])
    a[0] += wgt
    a[1] += wgt
    b = st.setdefault(_norm(loser), [0.0, 0.0])
    b[1] += wgt


def _add_season(sport: str, team: str, wins: int, losses: int,
                year: int) -> None:
    season_end = datetime(year, 12, 31).date()
    age = (_today() - season_end).days
    if age < 0:
        return
    wgt = _weight(float(age), sport)
    st = _store.setdefault(sport, {})
    a = st.setdefault(_norm(team), [0.0, 0.0])
    a[0] += wgt * wins
    a[1] += wgt * (wins + losses)


def _today():
    return datetime.now(timezone.utc).date()


def _load_games(path: Path, sport: str) -> int:
    if not path.exists():
        return 0
    n = 0
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            _add_game(sport, r.get("winner_name") or r.get("winner") or "",
                      r.get("loser_name") or r.get("loser") or "",
                      (r.get("date") or "").strip())
            n += 1
    return n


def _load_mlb(path: Path) -> int:
    if not path.exists():
        return 0
    n = 0
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                _add_season("mlb", r.get("team") or "", int(r["wins"]),
                            int(r["losses"]), int(r["year"]))
                n += 1
            except (KeyError, TypeError, ValueError):
                continue
    return n


def _load() -> None:
    global _loaded
    _load_games(_DATA / "history_tennis.csv", "tennis")
    _load_games(_DATA / "history_nba.csv", "nba")
    _load_games(_DATA / "history_nfl.csv", "nfl")
    _load_mlb(_DATA / "history_mlb.csv")
    _loaded = True


def sport_of_league(title: str) -> str | None:
    """Map a feed sport_title to a rated sport, or None."""
    t = (title or "").upper()
    if "NBA" in t:
        return "nba"
    if "MLB" in t:
        return "mlb"
    if "NFL" in t and "NCAAF" not in t:
        return "nfl"
    if "ATP" in t or "WTA" in t:
        return "tennis"
    return None


def pair_prob(sport: str, home: str, away: str) -> float | None:
    """P(home wins) via log5 from recency-weighted win rates."""
    if not _loaded:
        _load()
    st = _store.get(sport) or {}
    a = st.get(_norm(home))
    b = st.get(_norm(away))
    if not a or not b or a[1] < 4.0 or b[1] < 4.0:
        return None
    ra = min(max(a[0] / a[1], 0.25), 0.75)
    rb = min(max(b[0] / b[1], 0.25), 0.75)
    denom = ra + rb - 2.0 * ra * rb
    if denom <= 0:
        return None
    p = (ra - ra * rb) / denom + HOME_EDGE.get(sport, 0.0)
    return min(max(p, 0.05), 0.95)


if __name__ == "__main__":
    _load()
    for sport in ("tennis", "nba", "nfl", "mlb"):
        st = _store.get(sport) or {}
        top = sorted(st.items(), key=lambda kv: kv[1][1], reverse=True)[:5]
        print(f"  {sport}: {len(st)} rated | sample: "
              + ", ".join(f"{k} ({v[1]:.0f}g)" for k, v in top))