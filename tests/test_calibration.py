"""Calibration report tests — bucket arithmetic on synthetic ledger rows."""

from __future__ import annotations

import pytest

from utils.calibration import (
    bucket_label,
    brier_score,
    calibration_report,
    print_calibration,
    shrink,
)


def _rec(status: str, prob: float) -> dict:
    return {"status": status, "combined_prob": prob}


def test_bucket_labels() -> None:
    assert bucket_label(0.42) == "<50%"
    assert bucket_label(0.55) == "50%-59%"
    assert bucket_label(0.61) == "60%-69%"
    assert bucket_label(0.72) == "70%-79%"
    assert bucket_label(0.85) == "80%-89%"
    assert bucket_label(0.95) == "90%-100%"


def test_shrink_pulls_toward_half() -> None:
    assert shrink(0.80, 1.0) == pytest.approx(0.80)
    assert shrink(0.80, 0.5) == pytest.approx(0.65)
    assert shrink(0.40, 0.5) == pytest.approx(0.45)
    with pytest.raises(ValueError):
        shrink(0.8, 1.5)


def test_report_excludes_voids_from_actual_rate() -> None:
    rows = calibration_report([
        _rec("WIN", 0.80), _rec("WIN", 0.80), _rec("LOSS", 0.80),
        _rec("VOID", 0.80), _rec("PENDING", 0.80),
    ])
    b = next(r for r in rows if r["bucket"] == "80%-89%")
    assert b["n"] == 4          # pending excluded entirely
    assert b["wins"] == 2
    assert b["voids"] == 1
    assert b["actual"] == pytest.approx(2 / 3)
    assert b["stated"] == pytest.approx(0.80)


def test_report_empty_buckets_are_none() -> None:
    rows = calibration_report([_rec("WIN", 0.55)])
    dead = next(r for r in rows if r["bucket"] == "90%-100%")
    assert dead["stated"] is None and dead["actual"] is None


def test_brier_score() -> None:
    rows = [_rec("WIN", 1.0), _rec("LOSS", 0.0)]
    assert brier_score(rows) == pytest.approx(0.0)
    rows2 = [_rec("WIN", 0.5)]
    assert brier_score(rows2) == pytest.approx(0.25)
    assert brier_score([_rec("VOID", 0.9)]) is None
    assert brier_score([]) is None


def test_print_calibration_never_raises_on_empty(capsys) -> None:
    rows = print_calibration([])
    assert rows
    out = capsys.readouterr().out
    assert "CONFIDENCE CALIBRATION" in out
    assert "No settled bets yet" in out
