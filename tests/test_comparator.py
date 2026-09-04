"""EV math tests — hand-computed examples for edge, EV, fair odds, Kelly.

Reference example used throughout: p = 0.5, o = 2.2
    implied q   = 1/2.2 = 0.454545...
    EV per unit = 0.5 * 2.2 - 1 = +0.10
    edge        = 0.5 - 0.454545... = 0.045454...
    fair odds   = 1/0.5 = 2.00
    full Kelly  = (0.5*2.2 - 1) / (2.2 - 1) = 0.10/1.2 = 0.083333...
"""

from __future__ import annotations

import pytest

from odds.comparator import BetOpportunity, OddsComparator, OddsQuote, Selection


def _selection(p: float = 0.5, market: str = "1X2", pick: str = "Home") -> Selection:
    return Selection(
        match_id="ABC123",
        match_label="Arsenal vs Chelsea",
        league="Premier League",
        market=market,
        selection=pick,
        model_probability=p,
    )


def _opp(
    p: float = 0.5, o: float = 2.2, market: str = "1X2", pick: str = "Home"
) -> BetOpportunity:
    return OddsComparator().evaluate(
        _selection(p, market, pick), [OddsQuote("SportyBet", o)]
    )


# --------------------------------------------------------------------- #
# evaluate(): the four core numbers
# --------------------------------------------------------------------- #


def test_ev_edge_fair_odds_kelly_hand_computed() -> None:
    opp = _opp()
    assert opp.ev_per_unit == pytest.approx(0.10, abs=1e-12)
    assert opp.edge == pytest.approx(0.5 - 1.0 / 2.2, abs=1e-12)
    assert opp.fair_odds == pytest.approx(2.0)
    assert opp.kelly_full == pytest.approx(0.10 / 1.2, abs=1e-12)
    assert opp.implied_probability == pytest.approx(1.0 / 2.2)


def test_negative_ev_bet_has_negative_numbers() -> None:
    opp = _opp(p=0.4, o=2.0)  # fair would be 2.5; 2.0 is a bad price
    assert opp.ev_per_unit < 0
    assert opp.edge < 0
    assert opp.kelly_full < 0
    assert not opp.is_positive_ev


def test_evaluate_shops_the_best_price() -> None:
    quotes = [
        OddsQuote("Betika", 2.10),
        OddsQuote("SportyBet", 2.30),
        OddsQuote("BetPawa", 2.20),
    ]
    opp = OddsComparator().evaluate(_selection(), quotes)
    assert opp.book == "SportyBet"
    assert opp.decimal_odds == 2.30
    assert opp.quotes_by_book == {"Betika": 2.10, "SportyBet": 2.30, "BetPawa": 2.20}


def test_evaluate_requires_quotes() -> None:
    with pytest.raises(ValueError):
        OddsComparator().evaluate(_selection(), [])


def test_label_format() -> None:
    assert _opp().label == "Arsenal vs Chelsea | 1X2 -> Home"


# --------------------------------------------------------------------- #
# is_value() gating
# --------------------------------------------------------------------- #


def test_is_value_thresholds() -> None:
    cmp = OddsComparator(min_edge=0.03, min_ev_per_unit=0.03)
    assert cmp.is_value(_opp(p=0.5, o=2.2))            # edge 4.5%, EV 10%
    assert not cmp.is_value(_opp(p=0.46, o=2.2))       # edge ~0.5% — too thin
    assert not cmp.is_value(_opp(p=0.60, o=1.70))      # EV 2% < 3% floor


def test_is_value_respects_odds_and_prob_caps() -> None:
    cmp = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0, max_odds=3.0, min_model_prob=0.5)
    assert cmp.is_value(_opp(p=0.55, o=2.0))
    assert not cmp.is_value(_opp(p=0.55, o=3.5))  # odds above max_odds
    assert not cmp.is_value(_opp(p=0.45, o=2.5))  # prob below min_model_prob


def test_rank_orders_by_ev() -> None:
    cmp = OddsComparator()
    ranked = cmp.rank([_opp(p=0.4, o=2.2), _opp(p=0.5, o=2.4, market="ML")])
    assert ranked[0].ev_per_unit >= ranked[1].ev_per_unit


def test_find_value_bets_filters_and_ranks() -> None:
    cmp = OddsComparator(min_edge=0.03, min_ev_per_unit=0.03)
    good = (_selection(0.5), [OddsQuote("SportyBet", 2.2)])
    bad = (_selection(0.4), [OddsQuote("SportyBet", 2.2)])
    found = cmp.find_value_bets([bad, good])
    assert len(found) == 1
    assert found[0].selection.model_probability == 0.5


# --------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------- #


def test_quote_validation() -> None:
    with pytest.raises(ValueError):
        OddsQuote("SportyBet", 1.0)
    with pytest.raises(ValueError):
        OddsQuote("", 2.0)


def test_selection_probability_must_be_strict() -> None:
    with pytest.raises(ValueError):
        _selection(0.0)
    with pytest.raises(ValueError):
        _selection(1.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_edge": 1.0},
        {"min_edge": -0.1},
        {"min_ev_per_unit": -0.01},
        {"min_model_prob": 1.0},
        {"max_odds": 1.0},
    ],
)
def test_comparator_configuration_validation(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        OddsComparator(**kwargs)
