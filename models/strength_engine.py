"""
Team-strength engine — attack/defense ratings from historical results.

    v1: recency-weighted goal averages with shrinkage (documented below).
    This is the measured, honest path to sharper-than-consensus numbers;
    a full Dixon-Coles MLE is the planned v2 once history accumulates.

INPUT FILE — data/history.csv (owner exports from football-data.co.uk, free)
    Required columns (extra columns are ignored):

        date,league,home_team,away_team,home_goals,away_goals
        2026-08-16,Premier League,Arsenal,Chelsea,2,0

    * one row per FINISHED match; league names should match the feed's
      sport_title values (e.g. "Premier League") so league averages bind;
    * unknown league names still work: the engine falls back to the global
      weighted averages;
    * a template ships as data/history.csv.template -- rename it, fill it;
    * bad rows (missing goals, unparsable dates) are SKIPPED with a count,
      never fatal.  A missing file disables the engine entirely.

RATINGS (v1 algorithm)
    Every match gets a recency weight  w = 0.5 ** (age_days / half_life)
    (half_life default 180 days).  Per league:
        Lh = weighted mean home goals,  La = weighted mean away goals.
    Per team, split by side (home/away):
        baseline_for      = w_home * Lh + w_away * La
        baseline_against  = w_home * La + w_away * Lh
        attack_raw  = sum(w * goals_for)     / baseline_for
        defense_raw = sum(w * goals_against) / baseline_against
    Raw ratios are shrunk toward 1.0 by an effective-sample pseudo-count
        rating = 1 + (raw - 1) * (n_eff / (n_eff + PSEUDO_COUNT))
    so a 2-match sample barely moves the rating while a full season does.
    Ratings are clipped to [0.25, 2.8] for numerical safety.

EXPECTED GOALS (multiplicative parametrisation, matches the Poisson engine)
    lambda_home = Lh * attack_home * defense_away
    lambda_away = La * attack_away * defense_home
    exposed via expected_goals(home, away, league) -> ExpectedGoals.

TEAM MATCHING
    Names are normalised (lowercase, accents stripped, punctuation to
    spaces, common club suffixes dropped: FC/AFC/SC/CF/FK/IF/BK/SK/club).
    football-data.co.uk abbreviations ("Man City") vs feed names
    ("Manchester City") are handled by an OPTIONAL alias file
    data/team_aliases.csv with two columns: alias,canonical
        man city,manchester city
    Unmatched teams simply fall back to pure consensus upstream.
"""

from __future__ import annotations

import csv
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config.settings import DEFAULT_LEAGUE_AWAY_GOALS, DEFAULT_LEAGUE_HOME_GOALS
from models.probability_engine import ExpectedGoals

HISTORY_PATH = Path("data") / "history.csv"
ALIASES_PATH = Path("data") / "team_aliases.csv"

REQUIRED_COLUMNS = ("date", "league", "home_team", "away_team",
                    "home_goals", "away_goals")

#: Teams with fewer effective matches than this stay pinned near 1.0.
PSEUDO_COUNT: float = 8.0

#: Suffix tokens dropped during normalisation (case-insensitive).
_DROP_TOKENS = frozenset({"fc", "afc", "sc", "cf", "fk", "if", "bk", "sk",
                          "club", "the"})

#: Rating clip: no team is treated as 4x better/worse than league average.
_RATING_MIN, _RATING_MAX = 0.25, 2.80

#: Expected-goals clip keeps the Poisson engine in a sane regime.
_LAMBDA_MIN, _LAMBDA_MAX = 0.15, 4.50


def normalize_team_name(name: str) -> str:
    """Normalised team key: lowercase, de-accented, suffix-free."""
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = "".join(ch if ch.isalnum() else " " for ch in text.lower())
    tokens = [t for t in text.split() if t and t not in _DROP_TOKENS]
    return " ".join(tokens) if tokens else text.strip()


@dataclass(frozen=True)
class TeamRating:
    """Shrunk attack/defense multipliers (1.0 = league average)."""

    attack: float
    defense: float
    n_eff: float          # effective (recency-weighted) sample size
    last_played: str      # ISO date of the most recent match used


class StrengthEngine:
    """Recency-weighted attack/defense ratings per team + league averages."""

    def __init__(
        self,
        rows: list[dict[str, str]],
        half_life_days: float = 180.0,
        now: datetime | None = None,
    ) -> None:
        if half_life_days <= 0:
            raise ValueError("half_life_days must be positive.")
        self.half_life_days = float(half_life_days)
        self.now = now or datetime.now(timezone.utc)
        self.skipped_rows = 0
        self._league_sums: dict[str, dict[str, float]] = {}
        # (league, team) -> per-side weighted sums; side in {"home","away"}.
        self._team_sums: dict[tuple[str, str], dict[str, float]] = {}
        self._last_played: dict[tuple[str, str], str] = {}
        self._aliases = self._load_aliases(ALIASES_PATH)
        for row in rows:
            self._absorb(row)
        self._build()

    # ------------------------------ loading ----------------------------- #

    @staticmethod
    def _load_aliases(path: Path) -> dict[str, str]:
        """Optional alias -> canonical map (normalised keys)."""
        if not path.exists():
            return {}
        aliases: dict[str, str] = {}
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    alias = normalize_team_name(row.get("alias") or "")
                    canon = normalize_team_name(row.get("canonical") or "")
                    if alias and canon and alias != canon:
                        aliases[alias] = canon
        except (OSError, csv.Error):
            return {}
        return aliases

    def _key(self, name: str) -> str:
        key = normalize_team_name(name)
        return self._aliases.get(key, key)

    def _absorb(self, row: dict[str, str]) -> None:
        raw = {k: (row.get(k) or "").strip() for k in REQUIRED_COLUMNS}
        if any(not v for v in raw.values()):
            self.skipped_rows += 1
            return
        try:
            hg, ag = float(raw["home_goals"]), float(raw["away_goals"])
            if hg < 0 or ag < 0:
                raise ValueError
        except ValueError:
            self.skipped_rows += 1
            return
        try:
            played = datetime.fromisoformat(raw["date"][:10]).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            self.skipped_rows += 1
            return

        age_days = max(0.0, (self.now - played).total_seconds() / 86400.0)
        w = 0.5 ** (age_days / self.half_life_days)
        league = raw["league"]
        home = self._key(raw["home_team"])
        away = self._key(raw["away_team"])

        ls = self._league_sums.setdefault(league, {"w": 0.0, "hg": 0.0, "ag": 0.0})
        ls["w"] += w
        ls["hg"] += w * hg
        ls["ag"] += w * ag

        stamp = played.date().isoformat()
        for team, gf, ga, side in ((home, hg, ag, "home"), (away, ag, hg, "away")):
            ts = self._team_sums.setdefault(
                (league, team),
                {"w_home": 0.0, "w_away": 0.0,
                 "gf_home": 0.0, "gf_away": 0.0,
                 "ga_home": 0.0, "ga_away": 0.0})
            ts[f"w_{side}"] += w
            ts[f"gf_{side}"] += w * gf
            ts[f"ga_{side}"] += w * ga
            prev = self._last_played.get((league, team))
            if prev is None or stamp > prev:
                self._last_played[(league, team)] = stamp

    def _build(self) -> None:
        """League means first, then shrunk team ratings."""
        self.league_averages: dict[str, tuple[float, float]] = {}
        total_w = sum(ls["w"] for ls in self._league_sums.values())
        if total_w > 0:
            global_lh = sum(ls["hg"] for ls in self._league_sums.values()) / total_w
            global_la = sum(ls["ag"] for ls in self._league_sums.values()) / total_w
            for league, ls in self._league_sums.items():
                if ls["w"] > 0:
                    self.league_averages[league] = (ls["hg"] / ls["w"],
                                                    ls["ag"] / ls["w"])
        else:
            global_lh, global_la = DEFAULT_LEAGUE_HOME_GOALS, DEFAULT_LEAGUE_AWAY_GOALS
        self.global_average: tuple[float, float] = (global_lh, global_la)

        self._ratings: dict[tuple[str, str], TeamRating] = {}
        for (league, team), ts in self._team_sums.items():
            lh, la = self.league_averages.get(league, self.global_average)
            n_eff = ts["w_home"] + ts["w_away"]
            shrink = n_eff / (n_eff + PSEUDO_COUNT)
            baseline_for = ts["w_home"] * lh + ts["w_away"] * la
            baseline_against = ts["w_home"] * la + ts["w_away"] * lh
            goals_for = ts["gf_home"] + ts["gf_away"]
            goals_against = ts["ga_home"] + ts["ga_away"]
            attack_raw = (goals_for / baseline_for) if baseline_for > 0 else 1.0
            defense_raw = (goals_against / baseline_against) if baseline_against > 0 else 1.0
            attack = 1.0 + (attack_raw - 1.0) * shrink
            defense = 1.0 + (defense_raw - 1.0) * shrink
            self._ratings[(league, team)] = TeamRating(
                attack=min(max(attack, _RATING_MIN), _RATING_MAX),
                defense=min(max(defense, _RATING_MIN), _RATING_MAX),
                n_eff=round(n_eff, 3),
                last_played=self._last_played.get((league, team), ""),
            )

    # ------------------------------- public ------------------------------ #

    @property
    def ratings_table(self) -> dict[tuple[str, str], TeamRating]:
        return dict(self._ratings)

    def _find(self, team_key: str) -> TeamRating | None:
        """League-independent lookup; the best-sampled rating wins."""
        best: TeamRating | None = None
        for (_league, team), rating in self._ratings.items():
            if team == team_key and (best is None or rating.n_eff > best.n_eff):
                best = rating
        return best

    def rating(self, team: str, league: str | None = None) -> TeamRating | None:
        if league:
            return self._ratings.get((league, self._key(team)))
        return self._find(self._key(team))

    def has(self, home: str, away: str, league: str | None = None) -> bool:
        """True when BOTH teams have ratings."""
        if league:
            hk, ak = (league, self._key(home)), (league, self._key(away))
            return hk in self._ratings and ak in self._ratings
        return (self._find(self._key(home)) is not None
                and self._find(self._key(away)) is not None)

    def league_average(self, league: str) -> tuple[float, float]:
        """(avg home goals, avg away goals) with global fallback."""
        return self.league_averages.get(league, self.global_average)

    def expected_goals(self, home: str, away: str, league: str = "") -> ExpectedGoals:
        """Multiplicative Poisson parametrisation for one fixture.

        Teams without ratings count as league-average (1.0 multipliers), so
        a known team vs an unknown team still produces a usable, honest
        estimate -- but upstream blending only fires when BOTH are known.
        """
        lh, la = self.league_average(league) if league else self.global_average
        hr = self.rating(home, league or None) or self.rating(home)
        ar = self.rating(away, league or None) or self.rating(away)
        att_h = hr.attack if hr else 1.0
        def_h = hr.defense if hr else 1.0
        att_a = ar.attack if ar else 1.0
        def_a = ar.defense if ar else 1.0
        lambda_home = min(max(lh * att_h * def_a, _LAMBDA_MIN), _LAMBDA_MAX)
        lambda_away = min(max(la * att_a * def_h, _LAMBDA_MIN), _LAMBDA_MAX)
        return ExpectedGoals(home=lambda_home, away=lambda_away)


def load_engine(path: str | Path = HISTORY_PATH) -> StrengthEngine | None:
    """Engine from data/history.csv; None when missing/empty -- never raises."""
    src = Path(path)
    if not src.exists():
        return None
    try:
        with src.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = [r for r in csv.DictReader(fh)
                    if any((v or "").strip() for v in r.values())]
    except (OSError, csv.Error):
        return None
    if not rows:
        return None
    try:
        engine = StrengthEngine(rows)
    except Exception:
        return None
    return engine if engine._ratings else None


if __name__ == "__main__":
    engine = load_engine()
    if engine is None:
        print(f"  no usable history file (expected {HISTORY_PATH} -- see "
              f"data/history.csv.template for the required columns).")
        raise SystemExit(0)
    print(f"  strength engine: {len(engine._ratings)} team(s) rated, "
          f"{len(engine.league_averages)} league(s), "
          f"{engine.skipped_rows} row(s) skipped.")
    for league, (lh, la) in sorted(engine.league_averages.items()):
        print(f"    {league:<32} avg {lh:.2f}-{la:.2f}")
    top = sorted(
        engine._ratings.items(),
        key=lambda kv: kv[1].attack - kv[1].defense,
        reverse=True,
    )[:10]
    print("  top attack-minus-defense:")
    for (league, team), r in top:
        print(f"    {team:<28} att {r.attack:.2f} def {r.defense:.2f} "
              f"n_eff {r.n_eff:.1f} ({league})")
