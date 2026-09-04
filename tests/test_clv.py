"""CLV tests — metric arithmetic and the closing-snapshot round trip.

The snapshot is exercised against a stubbed feed (no network, no credits)
by monkeypatching utils.clv.OddsApiFeed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import utils.clv as clv
from odds.comparator import OddsQuote, Selection
from utils.clv import (
    closing_metrics,
    clv_summary_line,
    read_closing_records,
    snapshot_closing,
)
from utils.logger import BetLogger


class FakeFeed:
    """Offline stand-in for OddsApiFeed with one priced candidate."""

    markets = "h2h,totals"
    sports = ("soccer_epl",)

    def __init__(
        self,
        sports: tuple[str, ...] | None = None,
        min_hours_ahead: float = 0.0,
        max_hours_ahead: float = 1.0,
        **_kwargs: object,
    ) -> None:
        self.sports = sports or FakeFeed.sports

    @property
    def is_configured(self) -> bool:
        return True

    def sports_with_events_within(self, window_hours: float) -> tuple[str, ...]:
        return ("soccer_epl",)

    def collect(self, refresh: bool = False) -> list[tuple[Selection, list[OddsQuote]]]:
        selection = Selection(
            match_id="EVT123",
            match_label="Arsenal vs Chelsea · Wed 21:10 EAT",
            league="Premier League",
            market="1X2",
            selection="Home",
            model_probability=0.5,
        )
        return [(selection, [OddsQuote("Pinnacle", 1.90), OddsQuote("Betika", 2.00)])]


@pytest.fixture()
def fake_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clv, "OddsApiFeed", FakeFeed)


def _pending_record(bet_id: str, odds: float = 2.20) -> dict:
    return {
        "bet_id": bet_id,
        "status": "PENDING",
        "legs": [
            {
                "match_id": "EVT123",
                "market": "1X2",
                "selection": "Home",
                "decimal_odds": odds,
            }
        ],
    }


# --------------------------------------------------------------------- #
# Metrics arithmetic
# --------------------------------------------------------------------- #


def test_closing_metrics_empty() -> None:
    m = closing_metrics([])
    assert m == {"clv_legs": 0, "beat_close_rate": 0.0, "avg_clv": 0.0}


def test_closing_metrics_arithmetic() -> None:
    records = [
        {"clv": 0.10},
        {"clv": -0.05},
        {"clv": 0.05},
        {"no_clv_here": True},  # malformed rows are ignored
    ]
    m = closing_metrics(records)
    assert m["clv_legs"] == 3
    assert m["beat_close_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert m["avg_clv"] == pytest.approx((0.10 - 0.05 + 0.05) / 3, abs=1e-4)


def test_read_closing_records_missing_file(tmp_path: Path) -> None:
    assert read_closing_records(tmp_path / "nope.jsonl") == []


def test_read_closing_records_tolerates_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "closing.jsonl"
    path.write_text('{"clv": 0.1}\nnot json at all\n\n{"clv": -0.2}\n', encoding="utf-8")
    records = read_closing_records(path)
    assert [r["clv"] for r in records] == [0.1, -0.2]


def test_clv_summary_line() -> None:
    assert "no closing references" in clv_summary_line({"clv_legs": 0})
    line = clv_summary_line({"clv_legs": 4, "beat_close_rate": 0.75, "avg_clv": 0.031})
    assert "75%" in line and "+3.10%" in line


# --------------------------------------------------------------------- #
# Snapshot round trip (stubbed feed — no network)
# --------------------------------------------------------------------- #


def test_snapshot_records_clv_per_leg(
    tmp_path: Path, fake_feed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "closing.jsonl"
    records = snapshot_closing(
        [_pending_record("BET-A", odds=2.20)], path=path
    )
    assert len(records) == 1
    rec = records[0]
    # placed 2.20 vs best closing 2.00 -> CLV = +10%
    assert rec["closing_odds"] == pytest.approx(2.00)
    assert rec["clv"] == pytest.approx(0.10, abs=1e-6)
    assert rec["bet_id"] == "BET-A"


def test_snapshot_dedupes_legs(
    tmp_path: Path, fake_feed: None
) -> None:
    path = tmp_path / "closing.jsonl"
    pending = [_pending_record("BET-A")]
    first = snapshot_closing(pending, path=path)
    second = snapshot_closing(pending, path=path)
    assert len(first) == 1
    assert second == []
    assert len(read_closing_records(path)) == 1


def test_snapshot_skips_unpriced_markets(
    tmp_path: Path, fake_feed: None
) -> None:
    path = tmp_path / "closing.jsonl"
    pending = [
        _pending_record("BET-A"),
        {
            "bet_id": "BET-B",
            "status": "PENDING",
            "legs": [
                {
                    "match_id": "EVT999",   # not in the feed response
                    "market": "O/U 2.5",
                    "selection": "Over",
                    "decimal_odds": 1.85,
                }
            ],
        },
    ]
    records = snapshot_closing(pending, path=path)
    assert [r["bet_id"] for r in records] == ["BET-A"]


def test_snapshot_ignores_settled_bets(
    tmp_path: Path, fake_feed: None
) -> None:
    path = tmp_path / "closing.jsonl"
    rec = _pending_record("BET-A")
    rec["status"] = "WIN"
    assert snapshot_closing([rec], path=path) == []


def test_logger_metrics_includes_clv_keys(tmp_path: Path) -> None:
    logger = BetLogger(path=tmp_path / "bets.jsonl")
    m = logger.metrics()
    assert "beat_close_rate" in m and "avg_clv" in m
