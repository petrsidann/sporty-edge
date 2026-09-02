"""
CSV data pipeline — the two files YOU fill daily.

    data/fixtures.csv : one row per match to model (any supported sport)
    data/odds.csv     : one row per price you read on a platform

Unused columns can be left empty; documented defaults apply.  This is the
single seam where real data enters the system — everything downstream
(EV, slips, platform sheets, Telegram, ledger) is sport-agnostic.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from config.settings import (
    DEFAULT_LEAGUE_AWAY_GOALS,
    DEFAULT_LEAGUE_HOME_GOALS,
    LEAGUE_AVERAGE_GOALS,
)
from models.probability_engine import (
    PoissonMatchModel,
    TeamStrengths,
    expected_goals_from_strengths,
)
from models.sports import Log5MatchModel, MarginMatchModel
from models.tennis_engine import TennisInput, TennisMatchModel
from odds.comparator import OddsQuote, Selection

DATA_DIR = Path("data")
FIXTURES_PATH = DATA_DIR / "fixtures.csv"
ODDS_PATH = DATA_DIR / "odds.csv"

SUPPORTED_SPORTS = ("soccer", "tennis", "baseball", "basketball", "nfl")


def _to_float(row: dict[str, str], key: str, default: float) -> float:
    raw = (row.get(key) or "").strip()
    return float(raw) if raw else default


def fixture_label(row: dict[str, str]) -> str:
    return f"{row.get('home', '?')} vs {row.get('away', '?')}"


def load_fixtures(path: Path = FIXTURES_PATH) -> list[dict[str, str]]:
    """Read fixtures.csv; returns [] (with a message) when absent/empty."""
    if not path.exists():
        print(f"  !! {path} not found — create it from the template.")
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("match_id") or "").strip()]
    return rows


def load_odds(
    path: Path = ODDS_PATH,
) -> dict[tuple[str, str, str], list[OddsQuote]]:
    """Group odds.csv rows by (match_id, market, selection)."""
    grouped: dict[tuple[str, str, str], list[OddsQuote]] = {}
    if not path.exists():
        print(f"  !! {path} not found — no prices, nothing can be evaluated.")
        return grouped

    skipped = 0
    with path.open("r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                quote = OddsQuote(
                    book=(r.get("book") or "").strip(),
                    decimal_odds=float((r.get("odds") or "").strip()),
                )
            except (TypeError, ValueError):
                skipped += 1
                continue
            key = (
                (r.get("match_id") or "").strip(),
                (r.get("market") or "").strip(),
                (r.get("selection") or "").strip(),
            )
            grouped.setdefault(key, []).append(quote)
    if skipped:
        print(f"  !! skipped {skipped} odds rows (bad values or odds < 1.01).")
    return grouped


def build_model(row: dict[str, str]):
    """Dispatch one fixture row to the correct sport engine."""
    sport = (row.get("sport") or "").strip().lower()

    if sport == "soccer":
        league = (row.get("league") or "").strip()
        avg_home, avg_away = LEAGUE_AVERAGE_GOALS.get(
            league, (DEFAULT_LEAGUE_HOME_GOALS, DEFAULT_LEAGUE_AWAY_GOALS)
        )
        xg = expected_goals_from_strengths(
            avg_home,
            avg_away,
            TeamStrengths(
                attack=_to_float(row, "home_attack", 1.0),
                defense=_to_float(row, "home_defense", 1.0),
            ),
            TeamStrengths(
                attack=_to_float(row, "away_attack", 1.0),
                defense=_to_float(row, "away_defense", 1.0),
            ),
        )
        return PoissonMatchModel(xg)

    if sport == "tennis":
        return TennisMatchModel(
            TennisInput(
                home_player=row.get("home", "?"),
                away_player=row.get("away", "?"),
                pa=_to_float(row, "pa", 0.62),
                pb=_to_float(row, "pb", 0.62),
                best_of=int(_to_float(row, "best_of", 3)),
            )
        )

    if sport == "baseball":
        return Log5MatchModel(
            home_win_rate=_to_float(row, "home_win_rate", 0.5),
            away_win_rate=_to_float(row, "away_win_rate", 0.5),
        )

    if sport in ("basketball", "nfl"):
        default_sigma = 12.0 if sport == "basketball" else 13.5
        return MarginMatchModel(
            expected_margin=_to_float(row, "expected_margin", 0.0),
            sigma=_to_float(row, "sigma", default_sigma),
        )

    raise ValueError(f"Unknown sport '{sport}'. Supported: {SUPPORTED_SPORTS}")


def build_candidates(
    fixtures: list[dict[str, str]],
    odds_map: dict[tuple[str, str, str], list[OddsQuote]],
) -> tuple[dict[str, Any], list[tuple[Selection, list[OddsQuote]]]]:
    """Build models, then price every modelled market that has real quotes."""
    models: dict[str, Any] = {}
    candidates: list[tuple[Selection, list[OddsQuote]]] = []
    unpriced = 0

    for row in fixtures:
        match_id = (row.get("match_id") or "").strip()
        model = build_model(row)
        models[match_id] = model
        label = fixture_label(row)
        league = (row.get("league") or "").strip()

        for market_key, prob in model.market_probabilities().items():
            market, _, pick = market_key.partition(":")
            if prob <= 0.0 or prob >= 1.0:
                continue  # degenerate probability — not priceable
            quotes = odds_map.get((match_id, market, pick))
            if not quotes:
                unpriced += 1
                continue
            candidates.append(
                (
                    Selection(
                        match_id=match_id,
                        match_label=label,
                        league=league,
                        market=market,
                        selection=pick,
                        model_probability=prob,
                    ),
                    quotes,
                )
            )

    if unpriced:
        print(f"  ({unpriced} modelled selections had no odds entered — skipped.)")
    return models, candidates