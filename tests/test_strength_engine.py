"""Strength engine tests — ratings, shrinkage, expected goals, fallbacks.

Runs entirely offline against a temp history.csv.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from models.probability_engine import PoissonMatchModel
from models.strength_engine import (
    StrengthEngine,
    load_engine,
    normalize_team_name,
)

CSV = """date,league,home_team,away_team,home_goals,away_goals
2026-08-01,Premier League,Arsenal,Chelsea,3,0
2026-08-01,Premier League,Everton,Brighton,1,1
2026-08-02,Premier League,Chelsea,Arsenal,0,2
2026-08-02,Premier League,Brighton,Everton,0,0
2026-08-03,Premier League,Arsenal,Everton,2,1
2026-08-03,Premier League,Chelsea,Brighton,1,1
2026-08-04,La Liga,Real Madrid,Sevilla,3,1
2026-08-04,La Liga,Sevilla,Real Madrid,0,2
"""

BAD_CSV = """date,league,home_team,away_team,home_goals,away_goals
2026-08-01,Premier League,Good,Team,2,1
no-date-row,,Missing,Goals,x,y
2026-13-99,Premier League,Bad,Date,1,1
2026-08-02,Premier League,Neg,Score,-1,2
"""


def _rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(text.splitlines()))


def test_normalize_team_name_strips_suffixes_and_case() -> None:
    assert normalize_team_name("Manchester City FC") == "manchester city"
    assert normalize_team_name("  AFC  Bournemouth ") == "bournemouth"
    assert normalize_team_name("Réal Madrid") == "real madrid"


def test_ratings_counted_and_skipped_rows_tracked() -> None:
    engine = StrengthEngine(_rows(CSV))
    assert len(engine.ratings_table) == 6  # 4 EPL teams + Real Madrid + Sevilla
    assert engine.skipped_rows == 0
    bad = StrengthEngine(_rows(BAD_CSV))
    assert bad.skipped_rows == 3
    assert ("Premier League", "good") in bad._ratings


def test_goalscorer_gets_positive_attack_shrunk_below_one_hundred() -> None:
    engine = StrengthEngine(_rows(CSV))
    arsenal = engine.rating("Arsenal")
    wolves = engine.rating("Wolves")  # unknown -> None
    assert arsenal is not None
    assert arsenal.attack > 1.0
    assert arsenal.n_eff < 10  # tiny sample: shrunk, not saturated
    assert wolves is None


def test_has_requires_both_teams() -> None:
    engine = StrengthEngine(_rows(CSV))
    assert engine.has("Arsenal", "Everton")
    assert engine.has("Arsenal FC", "Everton")  # normalisation applies
    assert not engine.has("Arsenal", "Wolves")


def test_expected_goals_monotone_in_strength() -> None:
    engine = StrengthEngine(_rows(CSV))
    strong = engine.expected_goals("Arsenal", "Everton", "Premier League")
    reverse = engine.expected_goals("Everton", "Arsenal", "Premier League")
    assert strong.home > reverse.away  # same fixture, mirrored


def test_expected_goals_unknown_league_falls_back_to_global() -> None:
    engine = StrengthEngine(_rows(CSV))
    xg = engine.expected_goals("Arsenal", "Everton", "Unknown League X")
    assert xg.home > 0 and xg.away > 0


def test_model_from_engine_prices_all_markets() -> None:
    engine = StrengthEngine(_rows(CSV))
    xg = engine.expected_goals("Arsenal", "Everton", "Premier League")
    model = PoissonMatchModel(xg)
    probs = model.market_probabilities()
    assert abs(sum(model.one_x_two().values()) - 1.0) < 1e-9
    assert "O/U 2.5:Over" in probs


def test_load_engine_missing_file_is_none(tmp_path: Path) -> None:
    assert load_engine(tmp_path / "nope.csv") is None


def test_load_engine_real_file(tmp_path: Path) -> None:
    path = tmp_path / "history.csv"
    path.write_text(CSV, encoding="utf-8")
    engine = load_engine(path)
    assert engine is not None
    assert len(engine.ratings_table) == 6


def test_load_engine_corrupt_file_is_none(tmp_path: Path) -> None:
    path = tmp_path / "history.csv"
    path.write_text("not,a,real\ncsv,file,at,all\n", encoding="utf-8")
    assert load_engine(path) is None


def test_invalid_half_life_rejected() -> None:
    with pytest.raises(ValueError):
        StrengthEngine(_rows(CSV), half_life_days=0.0)
