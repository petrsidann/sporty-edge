"""Poisson soccer engine tests — exact derivations from the score matrix."""

from __future__ import annotations

import math

import pytest

from models.probability_engine import (
    ExpectedGoals,
    PoissonMatchModel,
    TeamStrengths,
    expected_goals_from_strengths,
)


def _model(home: float = 1.6, away: float = 1.2, max_goals: int = 10) -> PoissonMatchModel:
    return PoissonMatchModel(ExpectedGoals(home=home, away=away), max_goals=max_goals)


# --------------------------------------------------------------------- #
# 1X2
# --------------------------------------------------------------------- #


def test_one_x_two_sums_to_one() -> None:
    m = _model()
    p1x2 = m.one_x_two()
    assert sum(p1x2.values()) == pytest.approx(1.0, abs=1e-12)
    assert all(0.0 < p < 1.0 for p in p1x2.values())


def test_one_x_two_symmetric_for_equal_lambdas() -> None:
    m = _model(home=1.4, away=1.4)
    p1x2 = m.one_x_two()
    assert p1x2["Home"] == pytest.approx(p1x2["Away"], abs=1e-12)


def test_stronger_home_attack_raises_home_prob() -> None:
    weak = _model(home=1.1, away=1.4)
    strong = _model(home=2.2, away=0.9)
    assert strong.home_win_prob > weak.home_win_prob
    assert strong.away_win_prob < weak.away_win_prob


# --------------------------------------------------------------------- #
# O/U complement
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("line", [1.5, 2.5, 3.5])
def test_over_under_complement(line: float) -> None:
    ou = _model().over_under(line)
    assert ou["over"] + ou["under"] == pytest.approx(1.0, abs=1e-12)
    assert 0.0 < ou["over"] < 1.0


def test_over_prob_decreases_with_line() -> None:
    m = _model()
    assert (
        m.over_under(1.5)["over"]
        > m.over_under(2.5)["over"]
        > m.over_under(3.5)["over"]
    )


def test_over_under_rejects_integer_and_nonpositive_lines() -> None:
    m = _model()
    with pytest.raises(ValueError):
        m.over_under(2.0)  # integer lines carry push semantics — unsupported
    with pytest.raises(ValueError):
        m.over_under(0.0)


# --------------------------------------------------------------------- #
# BTTS — must match the closed form
# --------------------------------------------------------------------- #


def test_btts_matches_closed_form() -> None:
    lh, la = 1.6, 1.2
    m = _model(home=lh, away=la)
    closed_form_yes = (1.0 - math.exp(-lh)) * (1.0 - math.exp(-la))
    btts = m.both_teams_to_score()
    # The score matrix is renormalised for truncation, so allow a small delta.
    assert btts["yes"] == pytest.approx(closed_form_yes, abs=1e-6)
    assert btts["yes"] + btts["no"] == pytest.approx(1.0, abs=1e-12)


def test_btts_rises_with_expected_goals() -> None:
    low = _model(home=0.8, away=0.7).both_teams_to_score()["yes"]
    high = _model(home=2.4, away=2.1).both_teams_to_score()["yes"]
    assert high > low


# --------------------------------------------------------------------- #
# Double chance
# --------------------------------------------------------------------- #


def test_double_chance_sums_and_matches_1x2() -> None:
    m = _model()
    dc = m.double_chance()
    p1x2 = m.one_x_two()
    assert dc["1X"] == pytest.approx(p1x2["Home"] + p1x2["Draw"], abs=1e-12)
    assert dc["X2"] == pytest.approx(p1x2["Draw"] + p1x2["Away"], abs=1e-12)
    assert dc["12"] == pytest.approx(p1x2["Home"] + p1x2["Away"], abs=1e-12)
    # Exactly one of the three DC outcomes fails -> sum is 2 (each outcome
    # counted in exactly two of them).
    assert sum(dc.values()) == pytest.approx(2.0, abs=1e-12)


# --------------------------------------------------------------------- #
# Full market map
# --------------------------------------------------------------------- #


def test_market_probabilities_complete_and_complementary() -> None:
    m = _model()
    probs = m.market_probabilities()
    assert f"1X2:Home" in probs and f"BTTS:Yes" in probs
    for line in (1.5, 2.5, 3.5):
        assert probs[f"O/U {line}:Over"] + probs[f"O/U {line}:Under"] == pytest.approx(1.0)


# --------------------------------------------------------------------- #
# Edge cases and validation
# --------------------------------------------------------------------- #


def test_score_matrix_sums_to_one() -> None:
    assert _model().score_matrix.sum() == pytest.approx(1.0, abs=1e-12)


def test_invalid_expected_goals_rejected() -> None:
    with pytest.raises(ValueError):
        ExpectedGoals(home=0.0, away=1.0)
    with pytest.raises(ValueError):
        ExpectedGoals(home=1.0, away=11.0)


def test_invalid_strengths_rejected() -> None:
    with pytest.raises(ValueError):
        TeamStrengths(attack=0.0, defense=1.0)
    with pytest.raises(ValueError):
        TeamStrengths(attack=1.0, defense=-0.5)


def test_expected_goals_multiplicative_parametrisation() -> None:
    xg = expected_goals_from_strengths(
        1.5,
        1.2,
        TeamStrengths(attack=1.1, defense=0.9),
        TeamStrengths(attack=0.8, defense=1.2),
    )
    assert xg.home == pytest.approx(1.5 * 1.1 * 1.2)
    assert xg.away == pytest.approx(1.2 * 0.8 * 0.9)


@pytest.mark.parametrize("bad", [5, 21])
def test_max_goals_bounds_enforced(bad: int) -> None:
    with pytest.raises(ValueError):
        PoissonMatchModel(ExpectedGoals(home=1.5, away=1.2), max_goals=bad)
