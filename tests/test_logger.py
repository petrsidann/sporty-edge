"""Ledger round-trip tests — log_slip -> attach_code -> settle -> metrics.

Runs entirely against a temp file; the real data/bets.jsonl is never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slips.generator import Slip, SlipLeg
from utils.logger import BetLogger


def _leg(match_id: str = "M1", odds: float = 2.0, prob: float = 0.55) -> SlipLeg:
    return SlipLeg(
        match_id=match_id,
        match_label="A vs B",
        league="Premier League",
        market="1X2",
        selection="Home",
        book="SportyBet",
        decimal_odds=odds,
        model_prob=prob,
        edge=0.05,
        ev_per_unit=0.10,
    )


def _slip(stake: float = 1.0, slip_type: str = "SINGLE") -> Slip:
    return Slip(slip_type=slip_type, legs=[_leg()], stake_units=stake)


@pytest.fixture()
def logger(tmp_path: Path) -> BetLogger:
    return BetLogger(path=tmp_path / "bets.jsonl")


# --------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------- #


def test_log_attach_settle_round_trip(logger: BetLogger) -> None:
    bet_id = logger.log_slip(_slip(stake=2.0), session="MORNING")
    assert bet_id.startswith("BET-")

    rec = logger.attach_code(bet_id, "SportyBet", "ABC123")
    assert rec["platform"] == "SportyBet"
    assert rec["booking_code"] == "ABC123"

    settled = logger.settle(bet_id, "WIN")  # default payout = stake * odds
    assert settled["status"] == "WIN"
    assert settled["payout_units"] == pytest.approx(4.0)  # 2.0u at odds 2.0
    assert settled["profit_units"] == pytest.approx(2.0)


def test_settle_void_returns_stake(logger: BetLogger) -> None:
    bet_id = logger.log_slip(_slip(stake=1.0))
    settled = logger.settle(bet_id, "VOID")
    assert settled["payout_units"] == pytest.approx(1.0)
    assert settled["profit_units"] == pytest.approx(0.0)


def test_settle_loss_pays_nothing(logger: BetLogger) -> None:
    bet_id = logger.log_slip(_slip(stake=1.5))
    settled = logger.settle(bet_id, "LOSS")
    assert settled["payout_units"] == pytest.approx(0.0)
    assert settled["profit_units"] == pytest.approx(-1.5)


def test_settle_accepts_explicit_payout(logger: BetLogger) -> None:
    bet_id = logger.log_slip(_slip(stake=1.0))
    settled = logger.settle(bet_id, "WIN", payout_units=2.5)
    assert settled["payout_units"] == pytest.approx(2.5)


# --------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------- #


def test_double_settle_raises(logger: BetLogger) -> None:
    bet_id = logger.log_slip(_slip())
    logger.settle(bet_id, "WIN")
    with pytest.raises(ValueError):
        logger.settle(bet_id, "LOSS")


def test_invalid_outcome_raises(logger: BetLogger) -> None:
    bet_id = logger.log_slip(_slip())
    with pytest.raises(ValueError):
        logger.settle(bet_id, "PUSH")


def test_unknown_bet_id_raises(logger: BetLogger) -> None:
    with pytest.raises(KeyError):
        logger.settle("BET-NOPE", "WIN")
    with pytest.raises(KeyError):
        logger.attach_code("BET-NOPE", "SportyBet", "X")


def test_attach_code_requires_both_fields(logger: BetLogger) -> None:
    bet_id = logger.log_slip(_slip())
    with pytest.raises(ValueError):
        logger.attach_code(bet_id, "SportyBet", "")


# --------------------------------------------------------------------- #
# Metrics arithmetic
# --------------------------------------------------------------------- #


def test_metrics_arithmetic(logger: BetLogger) -> None:
    # 3 slips, 1u each at combined odds 2.0: WIN +1, LOSS -1, VOID 0.
    w = logger.log_slip(_slip(), session="MORNING")
    l = logger.log_slip(_slip(), session="MORNING")
    v = logger.log_slip(_slip(), session="EVENING")
    logger.settle(w, "WIN")
    logger.settle(l, "LOSS")
    logger.settle(v, "VOID")

    m = logger.metrics()
    assert m["total_bets"] == 3
    assert m["settled"] == 3
    assert m["pending"] == 0
    assert m["wins"] == 1
    assert m["win_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert m["total_staked_units"] == pytest.approx(3.0)
    assert m["profit_units"] == pytest.approx(0.0)
    assert m["roi"] == pytest.approx(0.0)


def test_metrics_counts_pending(logger: BetLogger) -> None:
    logger.log_slip(_slip())
    logger.log_slip(_slip())
    m = logger.metrics()
    assert m["total_bets"] == 2
    assert m["pending"] == 2
    assert m["settled"] == 0
    assert m["roi"] == 0.0  # no stake settled yet — no division by zero


def test_pending_and_setted_views(logger: BetLogger) -> None:
    a = logger.log_slip(_slip())
    b = logger.log_slip(_slip())
    logger.settle(a, "WIN")
    assert [r["bet_id"] for r in logger.pending()] == [b]
    assert [r["bet_id"] for r in logger.settled()] == [a]


def test_breakdown_by_slip_type(logger: BetLogger) -> None:
    a = logger.log_slip(_slip(slip_type="SINGLE"))
    logger.log_slip(_slip(slip_type="ACTION"))
    logger.settle(a, "WIN")
    df = logger.breakdown(by="slip_type")
    assert set(df["slip_type"]) == {"SINGLE"}  # only settled rows aggregate
    assert df["roi"].iloc[0] == pytest.approx(1.0)  # won 1u staked at 2.0


def test_ledger_file_written_atomically_and_readable(logger: BetLogger) -> None:
    bet_id = logger.log_slip(_slip())
    lines = logger.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1  # one JSON line per slip
    assert bet_id in lines[0]
