"""Feed parsing tests — the spreads market (P4).

Runs entirely offline: _parse_event works on plain event dicts, no API key
or network involved.
"""

from __future__ import annotations

import pytest

from config.settings import FeedSettings
from feeds.oddsapi import OddsApiFeed


def _event(markets: list[dict]) -> dict:
    return {
        "id": "evt1234567890",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "sport_title": "EPL",
        "commence_time": "2026-09-04T18:00:00Z",
        "bookmakers": [
            {"title": name, "markets": markets} for name in ("Pinnacle", "Betika")
        ],
    }


def _spread_market(home_price: float, away_price: float, point: float = -1.5) -> dict:
    return {
        "key": "spreads",
        "outcomes": [
            {"name": "Arsenal", "price": home_price, "point": point},
            {"name": "Chelsea", "price": away_price, "point": -point},
        ],
    }


def _feed(markets: str) -> OddsApiFeed:
    # markets is passed explicitly so the toggle is under test control.
    return OddsApiFeed(api_key="test-key", sports=("soccer_epl",), markets=markets)


def test_spreads_parsed_as_two_way_market() -> None:
    feed = _feed("h2h,totals,spreads")
    candidates = feed._parse_event(_event([_spread_market(1.95, 1.95)]))
    spreads = [sel for sel, _ in candidates if sel.market == "SPREAD 1.5"]
    assert {s.selection for s in spreads} == {"Home", "Away"}
    for sel, quotes in candidates:
        if sel.market == "SPREAD 1.5":
            assert len(quotes) == 2  # one price per book
            assert {q.book for q in quotes} == {"Pinnacle", "Betika"}


def test_spreads_probabilities_complement_and_sum() -> None:
    feed = _feed("h2h,totals,spreads")
    candidates = feed._parse_event(
        _event([_spread_market(2.10, 1.80), _spread_market(2.20, 1.72)])
    )
    by_pick = {
        sel.selection: sel.model_probability
        for sel, _ in candidates
        if sel.market == "SPREAD 1.5"
    }
    assert set(by_pick) == {"Home", "Away"}
    assert sum(by_pick.values()) == pytest.approx(1.0, abs=1e-9)
    assert all(0.02 <= p <= 0.98 for p in by_pick.values())


def test_spreads_consensus_is_median_across_books() -> None:
    feed = _feed("h2h,totals,spreads")
    books = [
        {"title": "B1", "markets": [_spread_market(2.00, 1.90)]},
        {"title": "B2", "markets": [_spread_market(2.20, 1.72)]},
        {"title": "B3", "markets": [_spread_market(2.40, 1.60)]},
    ]
    candidates = feed._parse_event(
        {"id": "evt1", "home_team": "Arsenal", "away_team": "Chelsea",
         "sport_title": "EPL", "commence_time": "2026-09-04T18:00:00Z",
         "bookmakers": books}
    )
    home = [
        sel.model_probability for sel, _ in candidates
        if sel.market == "SPREAD 1.5" and sel.selection == "Home"
    ]
    assert len(home) == 1  # one consensus value, not one per book


def test_spreads_require_min_books() -> None:
    feed = _feed("h2h,totals,spreads")
    event = _event([_spread_market(1.95, 1.95)])
    event["bookmakers"] = event["bookmakers"][:1]  # single book — below min
    candidates = feed._parse_event(event)
    assert not [sel for sel, _ in candidates if sel.market.startswith("SPREAD")]


def test_spreads_toggle_off_requests_no_spreads() -> None:
    """include_spreads=False must keep spreads out of the markets string."""
    feed_settings = FeedSettings(include_spreads=False)
    assert "spreads" not in feed_settings.effective_markets()
    assert feed_settings.effective_markets() == "h2h,totals"


def test_spreads_toggle_on_appends_market() -> None:
    feed_settings = FeedSettings()
    assert feed_settings.effective_markets() == "h2h,totals,spreads"


def test_spreads_ignored_when_not_requested() -> None:
    feed = _feed("h2h,totals")  # spreads never asked from the API
    candidates = feed._parse_event(_event([_spread_market(1.95, 1.95)]))
    assert not [sel for sel, _ in candidates if sel.market.startswith("SPREAD")]


def test_spreads_skips_outcomes_without_point() -> None:
    feed = _feed("h2h,totals,spreads")
    market = {
        "key": "spreads",
        "outcomes": [
            {"name": "Arsenal", "price": 1.95},            # point missing
            {"name": "Chelsea", "price": 1.95, "point": 1.5},
        ],
    }
    candidates = feed._parse_event(_event([market]))
    assert not [sel for sel, _ in candidates if sel.market.startswith("SPREAD")]
