"""De-margining tests — proportional (comparator) and power method (feed).

The power method must (a) renormalise to a proper probability distribution,
(b) shift probability FROM longshots TO favourites relative to the
proportional method (correcting the favourite-longshot bias), and
(c) work for 2-way and 3-way books.
"""

from __future__ import annotations

import pytest

from feeds.oddsapi import OddsApiFeed
from odds.comparator import OddsComparator


# --------------------------------------------------------------------- #
# Proportional method (OddsComparator.no_vig_probabilities)
# --------------------------------------------------------------------- #


def test_proportional_sums_to_one_two_way() -> None:
    probs = OddsComparator.no_vig_probabilities([1.9, 2.05])
    assert sum(probs) == pytest.approx(1.0, abs=1e-12)


def test_proportional_sums_to_one_three_way() -> None:
    probs = OddsComparator.no_vig_probabilities([2.1, 3.4, 3.6])
    assert sum(probs) == pytest.approx(1.0, abs=1e-12)


def test_proportional_equal_odds_gives_equal_probs() -> None:
    probs = OddsComparator.no_vig_probabilities([3.0, 3.0, 3.0])
    assert probs == pytest.approx([1 / 3, 1 / 3, 1 / 3])


def test_proportional_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        OddsComparator.no_vig_probabilities([])
    with pytest.raises(ValueError):
        OddsComparator.no_vig_probabilities([1.0, 2.0])  # odds must be >= 1.01


def test_margin_matches_overround() -> None:
    # 1/2.0 + 1/2.0 = 1.0 -> zero margin; adding a third 2.0 leg -> 0.5.
    assert OddsComparator.bookmaker_margin([2.0, 2.0]) == pytest.approx(0.0)
    assert OddsComparator.bookmaker_margin([2.0, 2.0, 2.0]) == pytest.approx(0.5)


# --------------------------------------------------------------------- #
# Power method (OddsApiFeed._devig)
# --------------------------------------------------------------------- #


def test_power_devig_sums_to_one_two_way() -> None:
    probs = OddsApiFeed._devig({"Home": 1.5, "Away": 2.5})
    assert probs is not None
    assert sum(probs.values()) == pytest.approx(1.0, abs=1e-9)


def test_power_devig_sums_to_one_three_way() -> None:
    probs = OddsApiFeed._devig({"Home": 2.1, "Draw": 3.4, "Away": 3.6})
    assert probs is not None
    assert sum(probs.values()) == pytest.approx(1.0, abs=1e-9)
    assert set(probs) == {"Home", "Draw", "Away"}


def test_power_devig_longshot_shrinks_vs_proportional() -> None:
    """Favourite-longshot bias: the power method must give the longshot
    LESS probability than proportional de-margining, and the favourite
    more."""
    odds = {"Home": 1.5, "Away": 2.5}
    power = OddsApiFeed._devig(odds)
    assert power is not None

    implied = {name: 1.0 / o for name, o in odds.items()}
    total = sum(implied.values())
    proportional = {name: q / total for name, q in implied.items()}

    assert power["Away"] < proportional["Away"]
    assert power["Home"] > proportional["Home"]


def test_power_devig_longshot_shrinks_three_way() -> None:
    odds = {"Home": 1.3, "Draw": 4.8, "Away": 8.0}
    power = OddsApiFeed._devig(odds)
    assert power is not None

    implied = {name: 1.0 / o for name, o in odds.items()}
    total = sum(implied.values())
    proportional = {name: q / total for name, q in implied.items()}

    # Both longshots shrink; the favourite absorbs the difference.
    assert power["Away"] < proportional["Away"]
    assert power["Draw"] < proportional["Draw"]
    assert power["Home"] > proportional["Home"]


def test_power_devig_monotone_in_price() -> None:
    """A shorter price always carries more probability than a longer one."""
    probs = OddsApiFeed._devig({"Home": 1.4, "Draw": 4.2, "Away": 7.5})
    assert probs is not None
    assert probs["Home"] > probs["Draw"] > probs["Away"]


def test_power_devig_edge_cases() -> None:
    # Fewer than 2 valid outcomes -> None (cannot de-margin).
    assert OddsApiFeed._devig({}) is None
    assert OddsApiFeed._devig({"Home": 1.9}) is None
    # Odds of 1.0 or worse are invalid and filtered out.
    assert OddsApiFeed._devig({"Home": 1.0, "Away": 2.0}) is None
    # Already-sub-fair book (sum of implied < 1): falls back to renormalised
    # implied probabilities rather than solving for k > 1.
    probs = OddsApiFeed._devig({"Home": 2.2, "Away": 2.3})
    assert probs is not None
    assert sum(probs.values()) == pytest.approx(1.0, abs=1e-12)
