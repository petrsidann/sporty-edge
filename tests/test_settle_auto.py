"""Regression tests for settle_auto.py _leg_result.

Phase 3a: verify ML/1X2/O/U/DC legs auto-settle from final scores.
Phase 3b: verify SPREAD legs now settle (market 'SPREAD {line}') instead
          of piling up PENDING forever.
"""
from __future__ import annotations

import pytest

from settle_auto import _leg_result


# --------------------------------------------------------------------- #
# Phase 3a: ML / 1X2 / OU / DC legs (regression — these already worked)
# --------------------------------------------------------------------- #

def test_ml_home_win() -> None:
    assert _leg_result(
        {"market": "1X2", "selection": "Home"}, {"home": 2, "away": 1}
    ) == "WIN"


def test_ml_home_loss() -> None:
    assert _leg_result(
        {"market": "1X2", "selection": "Home"}, {"home": 0, "away": 3}
    ) == "LOSS"


def test_ml_draw() -> None:
    assert _leg_result(
        {"market": "1X2", "selection": "Draw"}, {"home": 1, "away": 1}
    ) == "WIN"


def test_ml_away_win() -> None:
    assert _leg_result(
        {"market": "ML", "selection": "Away"}, {"home": 0, "away": 2}
    ) == "WIN"


def test_ou_over_win() -> None:
    assert _leg_result(
        {"market": "O/U 2.5", "selection": "Over"}, {"home": 2, "away": 2}
    ) == "WIN"


def test_ou_under_win() -> None:
    assert _leg_result(
        {"market": "O/U 2.5", "selection": "Under"}, {"home": 1, "away": 0}
    ) == "WIN"


def test_ou_under_loss() -> None:
    assert _leg_result(
        {"market": "O/U 2.5", "selection": "Under"}, {"home": 2, "away": 2}
    ) == "LOSS"


def test_dc_1x_win_on_draw() -> None:
    assert _leg_result(
        {"market": "DC", "selection": "1X"}, {"home": 1, "away": 1}
    ) == "WIN"


def test_dc_x2_win_on_away_win() -> None:
    assert _leg_result(
        {"market": "DC", "selection": "X2"}, {"home": 0, "away": 1}
    ) == "WIN"


def test_dc_12_win_on_home_win() -> None:
    assert _leg_result(
        {"market": "DC", "selection": "12"}, {"home": 2, "away": 1}
    ) == "WIN"


def test_dc_12_loss_on_draw() -> None:
    assert _leg_result(
        {"market": "DC", "selection": "12"}, {"home": 1, "away": 1}
    ) == "LOSS"


# --------------------------------------------------------------------- #
# Phase 3b: SPREAD legs (new — previously never auto-settled)
# --------------------------------------------------------------------- #

def test_spread_home_favorite_win() -> None:
    # Home -1.5: Home must win by > 1.5.  2-0 -> margin 2 > 1.5 -> WIN
    assert _leg_result(
        {"market": "SPREAD -1.5", "selection": "Home"}, {"home": 2, "away": 0}
    ) == "WIN"


def test_spread_home_favorite_loss() -> None:
    # Home -1.5: 1-0 -> margin 1, not > 1.5 -> LOSS
    assert _leg_result(
        {"market": "SPREAD -1.5", "selection": "Home"}, {"home": 1, "away": 0}
    ) == "LOSS"


def test_spread_home_underdog_win() -> None:
    # Home +1.5: Home gets 1.5.  Loses 0-1 -> margin -1 > -1.5 -> WIN
    assert _leg_result(
        {"market": "SPREAD +1.5", "selection": "Home"}, {"home": 0, "away": 1}
    ) == "WIN"


def test_spread_away_favorite_win() -> None:
    # Away -1.5: Away must win by > 1.5.  0-3 -> margin 3 > 1.5 -> WIN
    assert _leg_result(
        {"market": "SPREAD -1.5", "selection": "Away"}, {"home": 0, "away": 3}
    ) == "WIN"


def test_spread_away_underdog_win() -> None:
    # Away +1.5: Away gets 1.5.  Loses 2-1 -> margin -1 > -1.5 -> WIN
    assert _leg_result(
        {"market": "SPREAD +1.5", "selection": "Away"}, {"home": 2, "away": 1}
    ) == "WIN"


def test_spread_unparseable_line_returns_none() -> None:
    # No numeric line -> cannot judge -> None (handled by auto-VOID path)
    assert _leg_result(
        {"market": "SPREAD", "selection": "Home"}, {"home": 2, "away": 1}
    ) is None


def test_spread_unknown_selection_returns_none() -> None:
    assert _leg_result(
        {"market": "SPREAD -1.5", "selection": "Draw"}, {"home": 2, "away": 1}
    ) is None
