"""
void_phantoms.py - shows all pending slips in full, then voids the four
broken phantom tickets (the ones whose sheets printed empty).

    python void_phantoms.py
"""

from __future__ import annotations

from utils.logger import BetLogger

PHANTOMS = ("BET-D0E5BE2E", "BET-3483CF09", "BET-EFFC2A00", "BET-125C23F2")


def main() -> None:
    lg = BetLogger()

    print("=" * 66)
    print("  PENDING SLIPS (full detail)")
    print("=" * 66)
    for r in lg.pending():
        legs = " | ".join(
            f"{l['match_label']} {l['market']}->{l['selection']} "
            f"@ {l['decimal_odds']}"
            for l in r["legs"]
        )
        print(f"  {r['bet_id']}  [{r['slip_type']}]  odds {r['combined_odds']}  "
              f"stake {r['stake_units']}u")
        print(f"      {legs}")
        print()

    print("=" * 66)
    print("  VOIDING the 4 broken phantom tickets (sheets printed empty,")
    print("  so they were never placeable - VOID keeps the record honest)")
    print("=" * 66)
    for b in PHANTOMS:
        try:
            lg.settle(b, "VOID")
            print(f"  voided {b}")
        except (ValueError, KeyError) as exc:
            print(f"  skip {b}: {exc}")

    print()
    print("  Metrics:", lg.metrics())
    n = int(lg.metrics().get("settled", 0))
    print(f"  {n}/25 settled toward the first tier promotion.")


if __name__ == "__main__":
    main()