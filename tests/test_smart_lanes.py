"""smart_picks lane/rotation tests — pure functions, no feed, no network.

Covers the [HIT] lane (probability >= 0.80, odds <= 1.60, low/high totals
lines, derived double chance), the model blending fallbacks, and the
5-platform 3x2+2x1 slot rotation.
"""

from __future__ import annotations

import csv

import pytest

from models.strength_engine import StrengthEngine
from odds.comparator import OddsComparator, OddsQuote, Selection
from smart_picks import (
    _blend_with_model,
    _dc_candidates,
    _hit_candidates,
    _shrink_prob,
    platform_slots,
)
from utils.calibration import shrink


def _sel(market: str, pick: str, prob: float,
         label: str = "Arsenal vs Everton") -> Selection:
    return Selection(match_id="EVT1", match_label=label, league="Premier League",
                     market=market, selection=pick, model_probability=prob)


def _opp(market: str, pick: str, prob: float, odds: float,
         label: str = "Arsenal vs Everton", book: str = "Pinnacle"):
    return OddsComparator(min_edge=0.0, min_ev_per_unit=0.0).evaluate(
        _sel(market, pick, prob, label), [OddsQuote(book, odds)])


# --------------------------------------------------------------------- #
# [HIT] candidate filter
# --------------------------------------------------------------------- #

def test_hit_moneyline_needs_prob_and_odds_cap() -> None:
    h2h = {"EVT1": {"Home": _opp("1X2", "Home", 0.85, 1.45)}}
    assert len(_hit_candidates(h2h, {})) == 1

    h2h = {"EVT1": {"Home": _opp("1X2", "Home", 0.85, 1.70)}}  # odds too high
    assert _hit_candidates(h2h, {}) == []

    h2h = {"EVT1": {"Home": _opp("1X2", "Home", 0.70, 1.45)}}  # prob too low
    assert _hit_candidates(h2h, {}) == []


def test_hit_totals_only_on_the_low_and_high_lines() -> None:
    totals = {"EVT1": [
        _opp("O/U 0.5", "Over", 0.85, 1.30),
        _opp("O/U 2.5", "Over", 0.90, 1.45),   # 2.5 is NOT a HIT line
        _opp("O/U 4.5", "Under", 0.82, 1.35),
    ]}
    hits = _hit_candidates({}, totals)
    assert {(o.selection.market, o.selection.selection) for o, _ in hits} == {
        ("O/U 0.5", "Over"), ("O/U 4.5", "Under")}


def test_hit_double_chance_derived_from_one_x_two() -> None:
    h2h = {"EVT1": {
        "Home": _opp("1X2", "Home", 0.70, 1.40),
        "Draw": _opp("1X2", "Draw", 0.20, 4.50),
        "Away": _opp("1X2", "Away", 0.10, 8.00),
    }}
    dcs = _dc_candidates(h2h)
    by_pick = {o.selection.selection: o for o, _ in dcs}
    assert set(by_pick) == {"1X", "12"}       # X2 = 0.30, below the floor
    one_x = by_pick["1X"]
    assert one_x.selection.model_probability == pytest.approx(0.90)
    # derived reference = 1/0.90 * 0.95, conservatively below fair
    assert one_x.decimal_odds == pytest.approx((1 / 0.90) * 0.95, abs=1e-3)
    assert one_x.decimal_odds <= 1.60


def test_no_double_chance_without_a_draw_market() -> None:
    h2h = {"EVT1": {"Home": _opp("ML", "Home", 0.85, 1.40)}}
    assert _dc_candidates(h2h) == []


# --------------------------------------------------------------------- #
# Model blending
# --------------------------------------------------------------------- #

CSV = """date,league,home_team,away_team,home_goals,away_goals
2026-08-01,Premier League,Arsenal,Everton,3,0
2026-08-02,Premier League,Everton,Arsenal,0,2
2026-08-03,Premier League,Arsenal,Everton,2,1
2026-08-04,Premier League,Everton,Arsenal,1,2
2026-08-05,Premier League,Arsenal,Everton,2,0
2026-08-06,Premier League,Everton,Arsenal,0,1
"""


def _engine() -> StrengthEngine:
    return StrengthEngine(list(csv.DictReader(CSV.splitlines())))


def test_blend_mixes_model_and_consensus() -> None:
    opp = _opp("1X2", "Home", 0.80, 1.40)
    blended, flag = _blend_with_model(_engine(), opp)
    assert flag is True
    model_p = blended.selection.model_probability
    consensus_p = opp.selection.model_probability
    assert model_p != consensus_p


def test_blend_skips_unknown_teams() -> None:
    opp = _opp("1X2", "Home", 0.80, 1.40, label="Unknown A vs Unknown B")
    same, flag = _blend_with_model(_engine(), opp)
    assert flag is False and same is opp


def test_blend_skips_unmodelled_markets() -> None:
    opp = _opp("SPREAD 1.5", "Home", 0.60, 1.90)
    same, flag = _blend_with_model(_engine(), opp)
    assert flag is False and same is opp


def test_blend_survives_a_broken_engine() -> None:
    class Boom:
        def has(self, *a, **k):
            raise RuntimeError("boom")

    opp = _opp("1X2", "Home", 0.80, 1.40)
    same, flag = _blend_with_model(Boom(), opp)  # type: ignore[arg-type]
    assert flag is False and same is opp


def test_blend_requires_both_teams_known_for_totals() -> None:
    engine = _engine()
    opp = _opp("O/U 2.5", "Over", 0.55, 1.90)
    blended, flag = _blend_with_model(engine, opp)
    assert flag is True
    # The blend must actually move the number (0 < weight < 1), while
    # staying inside a sane probability band.
    assert blended.selection.model_probability != opp.selection.model_probability
    assert 0.05 <= blended.selection.model_probability <= 0.95


# --------------------------------------------------------------------- #
# Calibration shrinkage hook
# --------------------------------------------------------------------- #

def test_shrink_prob_is_identity_by_default() -> None:
    assert _shrink_prob(0.80) == pytest.approx(0.80)


def test_shrink_helper_matches_calibration_shrink() -> None:
    # settings default factor is 1.0, so both are identity; the maths
    # itself is covered in test_calibration.py
    assert _shrink_prob(0.9) == pytest.approx(shrink(0.9, 1.0))


# --------------------------------------------------------------------- #
# Phase 1: odds-band gate + flat stakes (concentrated-value pivot)
# --------------------------------------------------------------------- #

def test_odds_band_gate_bounds() -> None:
    from smart_picks import _in_odds_band
    assert _in_odds_band(1.55)          # inclusive lower edge
    assert _in_odds_band(2.60)          # inclusive upper edge
    assert _in_odds_band(1.90)          # the proven band
    assert not _in_odds_band(1.54)      # the -44.7% band stays shut
    assert not _in_odds_band(1.10)
    assert not _in_odds_band(2.61)


def test_settings_odds_band_matches_ledger_finding() -> None:
    from config.settings import MAX_ODDS_TAKEN, MIN_ODDS_TAKEN
    assert MIN_ODDS_TAKEN == 1.55
    assert MAX_ODDS_TAKEN == 2.60


def test_tier_is_flat_for_every_probability() -> None:
    from smart_picks import _tier
    for prob in (0.10, 0.55, 0.65, 0.80, 0.95):
        assert _tier(prob) == ("FLAT", 1.00)


def test_hit_lane_mode_is_disabled() -> None:
    # The ledger proved the sub-1.60 band bleeds (-44.7%); the HIT lane
    # must never come back by accident.
    from config.settings import HIT_RATE_MODE
    assert HIT_RATE_MODE is False


# --------------------------------------------------------------------- #
# Phase 2: the Telegram pick-message contract
# --------------------------------------------------------------------- #

def _contract_msg(**overrides):
    from smart_picks import build_pick_message
    kwargs = dict(lane="WINNER", model_tag=True, platform="BetPawa",
                  label="FLAT", match_label="Arsenal vs Everton · Tue 21:00 EAT",
                  kickoff_eat="Tue 21:00 EAT", settle_eat="23:03 EAT",
                  session_name="S3", pick="Arsenal", win_prob=0.62,
                  take_odds=1.90, floor_odds=1.84, stake=1.00,
                  bet_id="B-123")
    kwargs.update(overrides)
    return build_pick_message(**kwargs)


def test_pick_message_carries_the_full_telegram_contract() -> None:
    msg = _contract_msg()
    assert "Arsenal vs Everton" in msg              # match
    assert "Tue 21:00 EAT" in msg                   # kickoff EAT
    assert "settles ~23:03 EAT" in msg              # settle-by EAT
    assert "PICK: Arsenal" in msg                   # exact pick
    assert "place if app >= 1.84" in msg            # price floor
    assert "Win prob 62%" in msg                    # TRUE win probability
    assert "Stake 1.00u" in msg                     # stake
    assert "B-123" in msg                           # bet id
    assert "credits ~" in msg                       # credits remaining
    assert "guarantee" not in msg.lower()           # no guaranteed language


def test_pick_message_flags_derived_double_chance() -> None:
    msg = _contract_msg(lane="DC", pick="1X", derived_dc=True)
    assert "confirm the real DC price on the app" in msg


def test_pick_message_credits_line_is_honest_when_unknown() -> None:
    # No API call in this process -> credits show as '~?' (never invented).
    msg = _contract_msg()
    assert "credits ~?" in msg


# --------------------------------------------------------------------- #
# Platform rotation: 3 x 2 picks + 2 x 1, ledger-ranked
# --------------------------------------------------------------------- #

TARGETS = ["BetPawa", "Betika", "LuckyPari", "WekaWin", "BetJam"]


def test_rotation_default_order_without_history() -> None:
    slots = platform_slots({}, TARGETS)
    assert len(slots) == 8
    assert slots.count("BetPawa") == 2
    assert slots.count("Betika") == 2
    assert slots.count("LuckyPari") == 2
    assert slots.count("WekaWin") == 1
    assert slots.count("BetJam") == 1


def test_rotation_ranks_by_ledger_win_rate() -> None:
    perf = {
        "BetJam":   {"n": 20, "wins": 16, "staked": 10.0, "profit": 1.0},  # 80%
        "BetPawa":  {"n": 20, "wins": 14, "staked": 10.0, "profit": 0.5},  # 70%
        "Betika":   {"n": 20, "wins": 12, "staked": 10.0, "profit": 0.2},  # 60%
    }
    slots = platform_slots(perf, TARGETS)
    assert slots.count("BetJam") == 2      # best record earns the doubles
    assert slots.count("BetPawa") == 2
    assert slots.count("Betika") == 2
    assert slots.count("LuckyPari") == 1   # no history -> single, last
    assert slots.count("WekaWin") == 1
    assert slots[0] == "BetJam"            # best platform gets pick #1


def test_rotation_ignores_platforms_below_min_sample() -> None:
    perf = {"BetJam": {"n": 3, "wins": 3, "staked": 1.0, "profit": 1.0}}
    slots = platform_slots(perf, TARGETS)
    assert slots.count("BetJam") == 1      # 3 settled bets is not evidence


def test_rotation_never_drops_a_platform() -> None:
    perf = {name: {"n": 25, "wins": 20, "staked": 10.0, "profit": 2.0}
            for name in TARGETS}
    slots = platform_slots(perf, TARGETS)
    assert set(slots) == set(TARGETS)
    assert len(slots) == 8

