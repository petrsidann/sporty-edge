"""
postmortem.py - ledger post-mortem: where the money actually came from.

    python postmortem.py

Reads data/bets.jsonl and prints settled performance broken down by:
    (a) stake tier      (slip_type x exact stake in units)
    (b) platform        (recorded via BetLogger.attach_code)
    (c) market lane     (WINNER / TOTALS / DC / SPREAD / BTTS / MANUAL / MIXED)
    (d) odds band       (combined odds buckets)
    (e) sport / league  (first leg's league)
    (f) session         (S1..S6 / MIDNIGHT / MANUAL)

Rules of the output (honesty contract):
    * win rate  = wins / (wins + losses)   -- VOIDs excluded
    * turnover  = stakes of WIN + LOSS only -- VOID stake is returned, not risked
    * ROI       = profit / turnover
    * any segment with fewer than MIN_SAMPLE settled (W/L) bets is reported
      as "insufficient data" -- no conclusions are invented from 3 bets.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

LEDGER = Path("data") / "bets.jsonl"
CLOSING = Path("data") / "closing.jsonl"
MIN_SAMPLE = 10  # fewer settled W/L bets than this -> "insufficient data"

ODDS_BANDS: tuple[tuple[float, str], ...] = (
    (1.20, "< 1.20"),
    (1.60, "1.20-1.60"),
    (2.50, "1.60-2.50"),
    (5.00, "2.50-5.00"),
)


def odds_band(odds: float) -> str:
    for edge, label in ODDS_BANDS:
        if odds < edge:
            return label
    return "5.00+"


def leg_lane(market: str) -> str:
    m = (market or "").upper()
    if m.startswith(("ML", "MONEYLINE", "1X2")):
        return "WINNER"
    if m.startswith("O/U"):
        return "TOTALS"
    if m.startswith("DC"):
        return "DC"
    if m.startswith("SPREAD"):
        return "SPREAD"
    if m.startswith("BTTS"):
        return "BTTS"
    return (m or "OTHER")[:12]


def load_records(path: Path = LEDGER) -> list[dict]:
    if not path.exists():
        return []
    records: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                records.append(rec)
    return records


def _tier_key(rec: dict) -> str:
    st = rec.get("slip_type") or "?"
    stake = float(rec.get("stake_units") or 0.0)
    return f"{st} @ {stake:.3g}u"


def _rec_lane(rec: dict) -> str:
    legs = rec.get("legs") or []
    if not legs:
        return "OTHER"
    lanes = {leg_lane(str(leg.get("market", ""))) for leg in legs}
    if len(lanes) == 1:
        return lanes.pop()
    return "MIXED"


def _bucket(records: list[dict], key_fn) -> dict[str, dict]:
    groups: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "w": 0, "l": 0, "v": 0,
                 "staked": 0.0, "profit": 0.0, "prob": 0.0}
    )
    for rec in records:
        g = groups[key_fn(rec)]
        st = str(rec.get("status") or "PENDING")
        stake = float(rec.get("stake_units") or 0.0)
        profit = float(rec.get("profit_units") or 0.0)
        g["prob"] += float(rec.get("combined_prob") or 0.0)
        g["n"] += 1
        if st == "WIN":
            g["w"] += 1
            g["staked"] += stake
        elif st == "LOSS":
            g["l"] += 1
            g["staked"] += stake
        else:
            g["v"] += 1
        g["profit"] += profit
    return groups


def _print_table(title: str, groups: dict[str, dict]) -> list[str]:
    print(f"\n  --- {title} " + "-" * max(0, 56 - len(title)))
    header = (f"  {'segment':<28}{'n':>4}{'W':>4}{'L':>4}{'V':>4}"
              f"{'win%':>7}{'staked':>9}{'profit':>9}{'ROI%':>8}{'stated%':>9}")
    print(header)
    print("  " + "-" * (len(header) - 2))
    verdicts: list[str] = []
    rows = sorted(
        groups.items(),
        key=lambda kv: (kv[1]["w"] + kv[1]["l"] == 0, -kv[1]["profit"]),
    )
    for name, g in rows:
        settled = g["w"] + g["l"]
        win_rate = (g["w"] / settled * 100.0) if settled else 0.0
        roi = (g["profit"] / g["staked"] * 100.0) if g["staked"] else 0.0
        stated = (g["prob"] / g["n"] * 100.0) if g["n"] else 0.0
        note = "" if settled >= MIN_SAMPLE else "  (insufficient data)"
        print(f"  {name:<28}{g['n']:>4}{g['w']:>4}{g['l']:>4}{g['v']:>4}"
              f"{win_rate:>6.1f}%{g['staked']:>9.2f}{g['profit']:>+9.2f}"
              f"{roi:>+7.1f}%{stated:>+8.1f}%{note}")
        if settled >= MIN_SAMPLE:
            tag = "PROFIT" if g["profit"] > 0 and roi > 0 else (
                "BLEED" if g["profit"] < 0 else "FLAT")
            verdicts.append(f"{title}: {name} -> {tag} "
                            f"(ROI {roi:+.1f}% over {settled} settled)")
    if not verdicts:
        verdicts.append(f"{title}: insufficient data (no segment with "
                        f">= {MIN_SAMPLE} settled bets)")
    return verdicts


def main() -> None:
    print("=" * 74)
    print("  LEDGER POST-MORTEM | data/bets.jsonl")
    print("=" * 74)
    records = load_records()
    if not records:
        print(f"  {LEDGER} missing or empty -- nothing to analyse.")
        return

    settled = [r for r in records if r.get("status") != "PENDING"]
    wins = sum(1 for r in settled if r.get("status") == "WIN")
    losses = sum(1 for r in settled if r.get("status") == "LOSS")
    voids = len(settled) - wins - losses
    staked = sum(float(r.get("stake_units") or 0.0)
                 for r in settled if r.get("status") in ("WIN", "LOSS"))
    profit = sum(float(r.get("profit_units") or 0.0) for r in settled)
    pending = len(records) - len(settled)
    print(f"  bets {len(records)} | settled {len(settled)} "
          f"(W {wins} / L {losses} / V {voids}) | pending {pending}")
    if settled:
        wr = wins / (wins + losses) * 100.0 if (wins + losses) else 0.0
        roi = profit / staked * 100.0 if staked else 0.0
        print(f"  win rate {wr:.1f}% | turnover {staked:.2f}u | "
              f"profit {profit:+.2f}u | ROI {roi:+.1f}%")
    else:
        print("  No settled bets yet -- every segment below is pending.")

    all_verdicts: list[str] = []
    all_verdicts += _print_table(
        "(a) stake tier (slip_type x stake)",
        _bucket(settled, _tier_key))
    all_verdicts += _print_table(
        "(b) platform (attach_code)",
        _bucket(settled, lambda r: str(r.get("platform") or "unrecorded")))
    all_verdicts += _print_table(
        "(c) market lane",
        _bucket(settled, _rec_lane))
    all_verdicts += _print_table(
        "(d) odds band (combined)",
        _bucket(settled, lambda r: odds_band(float(r.get("combined_odds") or 0.0))))
    all_verdicts += _print_table(
        "(e) sport / league",
        _bucket(settled, lambda r: str(
            ((r.get("legs") or [{}])[0].get("league")) or "?")[:28]))
    all_verdicts += _print_table(
        "(f) session",
        _bucket(settled, lambda r: str(r.get("session") or "?")))

    print("\n  --- VERDICT " + "-" * 47)
    for v in all_verdicts:
        print(f"  * {v}")

    if CLOSING.exists():
        with CLOSING.open("r", encoding="utf-8") as fh:
            n = sum(1 for line in fh if line.strip())
        print(f"\n  CLV history: {CLOSING} exists ({n} rows).")
    else:
        print("\n  CLV history: none yet (data/closing.jsonl appears after the "
              "first closing snapshot).")
    print("=" * 74)


if __name__ == "__main__":
    main()
