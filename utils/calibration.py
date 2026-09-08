"""
Confidence calibration — does the system's stated win probability match
reality?

    python -m utils.calibration

Buckets every settled bet by its STATED win probability (combined_prob at
log time) and compares it with the ACTUAL win frequency per bucket.

Why it matters: if the system says 80% and reality is 65%, every Kelly
stake, tier multiplier and HIT-lane qualification built on that number is
wrong.  This report surfaces it loudly instead of hiding inside an average.

Rules (honesty contract):
    * VOIDs are excluded from the actual rate (neither wins nor losses).
    * A bucket with fewer than MIN_SAMPLE settled (W/L) bets is reported as
      "insufficient data" -- no conclusions from tiny samples.
    * The report NEVER edits the ledger; it only reads.

Shrinkage correction: CALIBRATION_SETTINGS.shrinkage_factor in
config/settings.py pulls every stated probability toward 0.5 before use,
    p' = 0.5 + (p - 0.5) * shrinkage_factor
Set it ONLY from this report's evidence, never by feel.
"""

from __future__ import annotations

from typing import Sequence

# Phase 4a: per-odds-band calibration bands (taken-odds from ledger legs).
# These mirror the ledger post-mortem's finding that only the 1.55-2.60 band
# is profitable.  The calibration report now tracks each band's ROI so the
# owner can see whether the concentrated-value pivot holds up over time.
ODDS_BAND_EDGES: tuple[float, ...] = (1.10, 1.55, 2.60, 99.99)
ODDS_BAND_LABELS: tuple[str, ...] = ("1.10-1.55", "1.55-2.60", "2.60+")

# Phase 4b: recommendation thresholds.
_STOP_ROI: float = -0.10      # band ROI < -10% over >=25 settled => stop
_SHRINK_ROI_FLOOR: float = -0.10
_SHRINK_ROI_CEIL: float = -0.05  # -10% to -5% => shrink 50%
_MIN_BAND_SAMPLE: int = 25    # settled W/L bets before a recommendation acts

# Probability-bucket calibration (stated vs actual win rate).
BUCKET_EDGES: tuple[float, ...] = (0.50, 0.60, 0.70, 0.80, 0.90, 1.01)
MIN_SAMPLE = 20  # settled W/L bets per bucket before a delta is actionable


def bucket_label(prob: float) -> str:
    """Bucket label for a stated probability; below 0.5 -> '<50%'."""
    if prob < BUCKET_EDGES[0]:
        return "<50%"
    for lo, hi in zip(BUCKET_EDGES, BUCKET_EDGES[1:]):
        if lo <= prob < hi:
            return f"{lo:.0%}-{hi - 0.01:.0%}"
    return "90%+"


def shrink(prob: float, factor: float) -> float:
    """Pull ``prob`` toward 0.5 by ``factor`` (1.0 = unchanged).

    Clamped into [0.01, 0.99] so downstream code never sees a degenerate 0
    or 1 (Selection forbids both, and 100% certainty does not exist).
    """
    if not 0.0 < factor <= 1.0:
        raise ValueError("factor must lie in (0, 1].")
    p = 0.5 + (prob - 0.5) * factor
    return min(max(p, 0.01), 0.99)


def calibration_report(records: Sequence[dict]) -> list[dict]:
    """Per-bucket calibration rows for settled bets.

    Each row: {"bucket", "n", "wins", "voids", "stated", "actual", "delta"}.
    ``stated``/``actual``/``delta`` are fractions in [0, 1]; None when the
    bucket has no settled (W/L) bets.
    """
    buckets: dict[str, dict] = {}
    order: list[str] = ["<50%"] + [
        f"{lo:.0%}-{hi - 0.01:.0%}" for lo, hi in zip(BUCKET_EDGES, BUCKET_EDGES[1:])
    ]
    for label in order:
        buckets[label] = {"bucket": label, "n": 0, "wins": 0, "voids": 0,
                          "prob_sum": 0.0}
    for rec in records:
        status = str(rec.get("status") or "")
        if status == "PENDING":
            continue
        prob = rec.get("combined_prob")
        if not isinstance(prob, (int, float)):
            continue
        b = buckets[bucket_label(float(prob))]
        b["n"] += 1
        b["prob_sum"] += float(prob)
        if status == "WIN":
            b["wins"] += 1
        elif status == "VOID":
            b["voids"] += 1

    rows: list[dict] = []
    for label in order:
        b = buckets[label]
        settled = b["n"] - b["voids"]
        stated = (b["prob_sum"] / b["n"]) if b["n"] else None
        actual = (b["wins"] / settled) if settled else None
        delta = (actual - stated) if (actual is not None and stated is not None) else None
        rows.append({
            "bucket": label,
            "n": b["n"],
            "wins": b["wins"],
            "voids": b["voids"],
            "stated": stated,
            "actual": actual,
            "delta": delta,
        })
    return rows


def _fmt_pct(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "  n/a"
    return f"{value * 100:+.1f}%" if signed else f"{value * 100:.1f}%"


def print_calibration(records: Sequence[dict]) -> list[dict]:
    """Print the calibration table; returns the rows for programmatic use."""
    rows = calibration_report(records)
    print("  CONFIDENCE CALIBRATION | stated vs actual win rate per bucket")
    print("  " + "-" * 66)
    print(f"  {'bucket':<10}{'n':>5}{'W':>5}{'V':>5}"
          f"{'stated':>10}{'actual':>10}{'delta':>10}")
    print("  " + "-" * 66)
    for r in rows:
        settled = r["n"] - r["voids"]
        note = "" if settled >= MIN_SAMPLE else "  (insufficient data)"
        print(f"  {r['bucket']:<10}{r['n']:>5}{r['wins']:>5}{r['voids']:>5}"
              f"{_fmt_pct(r['stated']):>10}{_fmt_pct(r['actual']):>10}"
              f"{_fmt_pct(r['delta'], signed=True):>10}{note}")

    # ---- the loud warning: buckets big enough to act on ------------------- #
    for r in rows:
        settled = r["n"] - r["voids"]
        if settled >= MIN_SAMPLE and r["delta"] is not None and r["delta"] <= -0.05:
            print(
                f"\n  !! OVERCONFIDENT: bucket {r['bucket']} states "
                f"{r['stated'] * 100:.1f}% but wins {r['actual'] * 100:.1f}% "
                f"({settled} settled). Every stake sized on that number is "
                f"too big. Consider CALIBRATION_SETTINGS.shrinkage_factor < 1.0 "
                f"in config/settings.py (e.g. 0.8) and re-run this report."
            )
            break
    if not any((r["n"] - r["voids"]) > 0 for r in rows):
        print("\n  No settled bets yet -- calibration needs settled W/L data.")
    return rows


def brier_score(records: Sequence[dict]) -> float | None:
    """Mean (outcome - stated_prob)^2 over settled W/L bets; None if empty.

    Lower is better; a constant 0.5 forecaster scores 0.25.  VOIDs excluded.
    """
    total = 0.0
    n = 0
    for rec in records:
        status = str(rec.get("status") or "")
        if status not in ("WIN", "LOSS"):
            continue
        prob = rec.get("combined_prob")
        if not isinstance(prob, (int, float)):
            continue
        outcome = 1.0 if status == "WIN" else 0.0
        total += (outcome - float(prob)) ** 2
        n += 1
    return (total / n) if n else None


    return (total / n) if n else None


def _odds_band_for(odds: float) -> str:
    """Map a taken-odds value to its band label."""
    for lo, hi, label in zip(
        ODDS_BAND_EDGES, ODDS_BAND_EDGES[1:], ODDS_BAND_LABELS
    ):
        if lo <= odds < hi:
            return label
    return ODDS_BAND_LABELS[-1]


def odds_band_report(records: Sequence[dict]) -> list[dict]:
    """Phase 4a: per-odds-band calibration table.

    Groups every settled (W/L) bet by its taken odds (first leg's
    ``decimal_odds``) into the configured bands and computes wins, staked,
    profit and ROI per band.  VOIDs are excluded.  Returns one row per band:
    ``{\"band\", \"n\", \"wins\", \"staked\", \"profit\", \"roi\"}``.
    """
    bands: dict[str, dict] = {}
    for label in ODDS_BAND_LABELS:
        bands[label] = {"band": label, "n": 0, "wins": 0,
                        "staked": 0.0, "profit": 0.0}

    for rec in records:
        status = str(rec.get("status") or "")
        if status not in ("WIN", "LOSS"):
            continue
        legs = rec.get("legs") or []
        if not legs:
            continue
        odds = legs[0].get("decimal_odds")
        if not isinstance(odds, (int, float)) or odds <= 1.0:
            continue
        band = bands[_odds_band_for(float(odds))]
        band["n"] += 1
        band["staked"] += float(rec.get("stake_units") or 0.0)
        band["profit"] += float(rec.get("profit_units") or 0.0)
        if status == "WIN":
            band["wins"] += 1

    rows: list[dict] = []
    for label in ODDS_BAND_LABELS:
        b = bands[label]
        roi = (b["profit"] / b["staked"]) if b["staked"] > 0 else None
        rows.append({**b, "roi": roi})
    return rows


def band_recommendation(roi: float | None, n: int) -> str:
    """Phase 4b: one-line recommendation per odds band.

    Rules (from the owner's ledger post-mortem):
        - band ROI < -10% over >=25 settled => \"stop\"
        - -10% to -5% => \"shrink 50%\"
        - positive (or insufficient data) => \"continue\"
    """
    if roi is None or n < _MIN_BAND_SAMPLE:
        return "continue (insufficient data)"
    if roi < _STOP_ROI:
        return "stop"
    if _SHRINK_ROI_FLOOR <= roi < _SHRINK_ROI_CEIL:
        return "shrink 50%"
    return "continue"


def print_odds_band_calibration(records: Sequence[dict]) -> list[dict]:
    """Print the per-odds-band table with one-line recommendations.
    Returns the rows for programmatic use (e.g. session_cycle)."""
    rows = odds_band_report(records)
    print("\n  PER-ODDS-BAND CALIBRATION | taken-odds vs ROI per band")
    print("  " + "-" * 60)
    print(f"  {'band':<12}{'n':>5}{'wins':>6}{'staked':>9}"
          f"{'profit':>9}{'roi':>9}  recommendation")
    print("  " + "-" * 60)
    for r in rows:
        roi_s = f"{r['roi'] * 100:+.1f}%" if r["roi"] is not None else "  n/a"
        print(f"  {r['band']:<12}{r['n']:>5}{r['wins']:>6}"
              f"{r['staked']:>9.2f}{r['profit']:>9.2f}"
              f"{roi_s:>9}  {band_recommendation(r['roi'], r['n'])}")
    return rows


def calibration_one_liner(records: Sequence[dict]) -> str:
    """Phase 4c: a single line summarising the calibration recommendation
    for the active (1.55-2.60) band — intended for the daily Telegram."""
    rows = odds_band_report(records)
    active = next((r for r in rows if r["band"] == "1.55-2.60"), None)
    if active is None or active["roi"] is None:
        return "calibration: 1.55-2.60 band — insufficient data, continue"
    rec = band_recommendation(active["roi"], active["n"])
    return (
        f"calibration 1.55-2.60 band: n={active['n']} "
        f"ROI={active['roi'] * 100:+.1f}% -> {rec}"
    )


def main() -> None:
    from utils.logger import BetLogger

    records = BetLogger()._read_all()
    if not records:
        print("  Ledger empty -- nothing to calibrate yet.")
        return
    rows = print_calibration(records)
    bs = brier_score(records)
    if bs is not None:
        print(f"\n  Brier score: {bs:.4f}  (0 = perfect, 0.25 = coin-flip "
              f"baseline; lower is better)")
    print_odds_band_calibration(records)
    print(f"\n  Daily signal: {calibration_one_liner(records)}")
    print("  Shrinkage option: CALIBRATION_SETTINGS.shrinkage_factor in "
          "config/settings.py (1.0 = off).")


if __name__ == "__main__":
    main()
