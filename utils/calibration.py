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
    print("  Shrinkage option: CALIBRATION_SETTINGS.shrinkage_factor in "
          "config/settings.py (1.0 = off).")


if __name__ == "__main__":
    main()
