"""
build_slip.py - interactive multi-match accumulator builder.

    python build_slip.py

Add legs one at a time - any match, any league, any sport:

    match label     : Arsenal vs Chelsea   (free text)
    decimal odds    : 1.91                 (the price you will actually take)
    win probability : 0.55                 (from market_scan fair odds = 1/fair,
                                            a blitz-table Model %, or your read)

Type 'undo' at the match prompt to remove the last leg.  Empty label = finish.

The builder multiplies the legs into ONE slip: combined odds, combined TRUE
win probability, EV vs break-even, a booking sheet ready for your app's
Book button, and a ledger entry (session MANUAL) so you settle it later.

Legs MULTIPLY - nothing is sure.  Every slip prints its true landing
probability next to the combined odds, so you always stake with open eyes.
"""

from __future__ import annotations

import uuid

from config.settings import PLACEABLE_BOOKS
from slips.generator import Slip, SlipLeg
from slips.platform_slip import choose_platform
from utils.logger import BetLogger


def _ask_float(prompt: str, lo: float, hi: float) -> float:
    """Prompt until a valid float in [lo, hi] is entered."""
    while True:
        raw = input(prompt).strip().replace(",", ".")
        try:
            value = float(raw)
        except ValueError:
            print("    X enter a number (e.g. 1.91).")
            continue
        if not lo <= value <= hi:
            print(f"    X must be between {lo} and {hi}.")
            continue
        return value


def _verdict(ev: float) -> str:
    """Honest per-leg label."""
    if ev >= 0.02:
        return "VALUE"
    if ev > 0.0:
        return "thin+"
    return "NEGATIVE EV"


def main() -> None:
    print("=" * 66)
    print("  sporty-edge - multi-match slip builder")
    print("=" * 66)
    print("  Add legs (any match, any sport). Type 'undo' to remove the")
    print("  last leg. Empty match name = finish and build.\n")

    legs: list[SlipLeg] = []

    while True:
        label = input("  Match (e.g. 'Real Sociedad vs Celta'): ").strip()
        if not label:
            break
        if label.lower() == "undo":
            if legs:
                removed = legs.pop()
                print(f"    removed: {removed.match_label}")
            else:
                print("    nothing to undo.")
            continue

        odds = _ask_float("  Decimal odds you will take           : ", 1.01, 1000.0)
        prob = _ask_float("  Model win probability (0-1, e.g. 0.55): ", 0.005, 0.995)

        edge = prob - 1.0 / odds
        ev = prob * odds - 1.0
        print(
            f"    -> fair {1.0 / prob:.2f} | edge {edge * 100:+.1f}% | "
            f"EV {ev * 100:+.1f}%  [{_verdict(ev)}]"
        )

        legs.append(
            SlipLeg(
                match_id="MAN-" + uuid.uuid4().hex[:6].upper(),
                match_label=label,
                league="manual",
                market="MANUAL",
                selection="PICK",
                book="manual entry",
                decimal_odds=odds,
                model_prob=prob,
                edge=edge,
                ev_per_unit=ev,
            )
        )

    if not legs:
        print("\n  No legs entered - nothing to build.")
        return

    combined_odds = 1.0
    combined_prob = 1.0
    for leg in legs:
        combined_odds *= leg.decimal_odds
        combined_prob *= leg.model_prob

    ev_total = combined_prob * combined_odds - 1.0
    breakeven_prob = 1.0 / combined_odds

    print("\n" + "=" * 66)
    print(f"  SLIP: {len(legs)} leg(s)")
    print("=" * 66)
    for i, leg in enumerate(legs, start=1):
        flag = "[+]" if leg.ev_per_unit >= 0.02 else ("[~]" if leg.ev_per_unit > 0 else "[-]")
        print(
            f"  {i}. {flag} {leg.match_label:<40} @ {leg.decimal_odds:<6.2f} "
            f"(model {leg.model_prob * 100:.0f}%, EV {leg.ev_per_unit * 100:+.1f}%)"
        )
    print("-" * 66)
    print(f"  Combined odds      : {combined_odds:.2f}")
    print(
        f"  TRUE win prob      : {combined_prob * 100:.1f}%"
        f"  ->  expected to land ~{combined_prob * 10:.0f} of 10"
    )
    print(
        f"  Break-even prob    : {breakeven_prob * 100:.1f}% "
        f"(the probability this slip needs to be worth staking)"
    )
    print(f"  Slip EV            : {ev_total * 100:+.1f}%  [{_verdict(ev_total)}]")
    if ev_total <= 0:
        print(
            "  WARNING: this slip is NEGATIVE EV on your own numbers - the"
            "\n  combined price pays less than the true chance of it landing."
        )

    default_stake = 0.5
    raw = input(f"\n  Stake in units [Enter = {default_stake}]: ").strip()
    try:
        stake = float(raw) if raw else default_stake
    except ValueError:
        stake = default_stake
    if stake <= 0:
        print("  Stake must be positive - slip not logged.")
        return

    slip = Slip(
        slip_type="ACCA" if len(legs) >= 2 else "SINGLE",
        legs=legs,
        stake_units=stake,
    )
    logger = BetLogger()
    bet_id = logger.log_slip(slip, session="MANUAL")
    print(f"\n  -> logged as {bet_id} (PENDING, MANUAL)")

    sheet = choose_platform(slip)
    platform = PLACEABLE_BOOKS[0] if PLACEABLE_BOOKS else "your app"
    if sheet is not None:
        print(sheet.render(bet_id=bet_id))
    else:
        text = slip.render()
        text = text.replace(
            f" SLIP {slip.slip_id}",
            f" LOAD ON >>> {platform.upper()}\n SLIP {slip.slip_id}",
            1,
        )
        print(text)

    print(
        f"\n  NEXT: open {platform}, add each pick at >= its stated odds,"
        "\n  set the stake, tap Book, then register the code:"
        f"\n    python -c \"from utils.logger import BetLogger; "
        f"BetLogger().attach_code('{bet_id}','{platform}','CODE')\""
        "\n  Settle after the games:  python settle.py"
    )


if __name__ == "__main__":
    main()