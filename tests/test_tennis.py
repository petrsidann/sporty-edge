"""Tennis i.i.d. point-model tests — the chain must be exact and bounded.

Regression focus: the tiebreak used to hit a RecursionError on long deuce
loops; every probability here must stay inside (0, 1) at all input bounds
and terminate instantly.
"""

from __future__ import annotations

import pytest

from models.tennis_engine import (
    TennisInput,
    TennisMatchModel,
    game_win_prob,
    match_win_prob,
    set_win_prob,
    tiebreak_win_prob,
)


# --------------------------------------------------------------------- #
# Game
# --------------------------------------------------------------------- #


def test_game_prob_in_unit_interval() -> None:
    for p in (0.05, 0.3, 0.5, 0.62, 0.95):
        assert 0.0 < game_win_prob(p) < 1.0


def test_game_prob_at_fifty_fifty_is_exactly_half() -> None:
    # Hand-check: p=0.5 -> p^4(1+4q+10q^2) + 20p^3q^3 * p^2/(1-2pq) = 0.5.
    assert game_win_prob(0.5) == pytest.approx(0.5, abs=1e-12)


def test_game_prob_monotone_in_point_prob() -> None:
    assert game_win_prob(0.7) > game_win_prob(0.6) > game_win_prob(0.5)


# --------------------------------------------------------------------- #
# Tiebreak
# --------------------------------------------------------------------- #


def test_tiebreak_symmetric_at_equal_point_probs() -> None:
    assert tiebreak_win_prob(0.7, 0.7) == pytest.approx(0.5, abs=1e-9)
    assert tiebreak_win_prob(0.5, 0.5) == pytest.approx(0.5, abs=1e-9)


def test_tiebreak_in_unit_interval_at_extremes() -> None:
    # The former RecursionError case: long win-by-2 loops at extreme inputs.
    for pa in (0.05, 0.5, 0.95):
        for pb in (0.05, 0.5, 0.95):
            assert 0.0 <= tiebreak_win_prob(pa, pb) <= 1.0


def test_tiebreak_better_server_wins_more() -> None:
    assert tiebreak_win_prob(0.8, 0.5) > 0.5
    assert tiebreak_win_prob(0.5, 0.8) < 0.5


# --------------------------------------------------------------------- #
# Set
# --------------------------------------------------------------------- #


def test_set_prob_in_unit_interval() -> None:
    for pa in (0.05, 0.5, 0.95):
        for pb in (0.05, 0.5, 0.95):
            s = set_win_prob(pa, pb)
            # Extreme mismatches saturate to the bounds in floating point.
            assert 0.0 <= s <= 1.0
            if pa == pb:
                assert 0.0 < s < 1.0


def test_set_equal_point_probs_slight_first_server_edge() -> None:
    # Home serves first, so at equal point probabilities the first server
    # carries a modest advantage — real, but nowhere near a free win.
    s = set_win_prob(0.62, 0.62)
    assert 0.5 < s < 0.65


# --------------------------------------------------------------------- #
# Match
# --------------------------------------------------------------------- #


def test_match_prob_in_unit_interval() -> None:
    for pa in (0.05, 0.35, 0.5, 0.75, 0.95):
        for best_of in (3, 5):
            assert 0.0 < match_win_prob(pa, 0.6, best_of) < 1.0


def test_higher_pa_gives_higher_match_prob() -> None:
    probs = [match_win_prob(pa, pb=0.6, best_of=3) for pa in (0.5, 0.6, 0.7, 0.8)]
    assert probs == sorted(probs)
    assert len(set(probs)) == len(probs)  # strictly increasing


def test_best_of_five_favours_the_stronger_player() -> None:
    assert match_win_prob(0.65, 0.6, best_of=5) > match_win_prob(0.65, 0.6, best_of=3)


def test_match_equal_players_stays_bounded() -> None:
    # Equal players with home serving first: the serve edge compounds but
    # the match must still be a coin-flip-ish contest, not a formality.
    m3 = match_win_prob(0.6, 0.6, best_of=3)
    m5 = match_win_prob(0.6, 0.6, best_of=5)
    assert 0.5 < m3 < 0.65
    assert 0.5 < m5 < 0.70
    assert m5 > m3  # longer format favours the (slightly) better-positioned


# --------------------------------------------------------------------- #
# Model wrapper + validation
# --------------------------------------------------------------------- #


def test_tennis_model_market_probabilities_complement() -> None:
    model = TennisMatchModel(TennisInput("A", "B", pa=0.63, pb=0.59, best_of=3))
    probs = model.market_probabilities()
    assert probs["ML:Home"] + probs["ML:Away"] == pytest.approx(1.0, abs=1e-12)
    assert model.summary()  # smoke: renders without error


def test_tennis_input_validation() -> None:
    with pytest.raises(ValueError):
        TennisInput("A", "B", pa=0.04, pb=0.5)   # below the 0.05 floor
    with pytest.raises(ValueError):
        TennisInput("A", "B", pa=0.5, pb=0.96)   # above the 0.95 ceiling
    with pytest.raises(ValueError):
        TennisInput("A", "B", pa=0.5, pb=0.5, best_of=4)
