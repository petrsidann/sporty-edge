"""
settle.py — interactive settlement of the bet ledger.

Walks every PENDING bet, shows its legs (pick + odds) and stake, and asks
for the result.  Settlement rules match the ledger defaults:

    WIN  pays stake * combined odds
    VOID returns the stake
    LOSS pays nothing

After the last bet it prints the running metrics and a per-slip-type
breakdown so you can see which tiers are actually paying.

Usage (from the repo root):
    python settle.py                        # settle every PENDING bet
    python settle.py --ledger other.jsonl   # operate on a different ledger
Answers per bet:  w = WIN, l = LOSS, v = VOID, Enter = skip, q = quit.
Bad input never settles anything and never crashes the session.
"""

from __future__ import annotations

import sys

from utils.logger import BetLogger

_ANSWERS: dict[str, str] = {
    "w": "WIN",
    "win": "WIN",
    "l": "LOSS",
    "loss": "LOSS",
    "v": "VOID",
    "void": "VOID",
}


def _banner(title: str) -> None:
    line = "=" * 74
    print(f"\n{line}\n  {title}\n{line}")


def _render_bet(rec: dict, index: int, total: int) -> None:
    """Print one pending bet with its legs, exactly as it was logged."""
    print(f"\n[{index}/{total}] {rec['bet_id']}  {rec['slip_type']}  "
          f"odds {rec['combined_odds']:.2f}  stake {rec['stake_units']:.2f}u  "
          f"(logged {rec['logged_at']}, session {rec.get('session') or '-'})")
    for i, leg in enumerate(rec.get("legs", []), start=1):
        print(
            f"  leg {i}: {leg.get('match_label', '?')}  "
            f"[{leg.get('league', '')}]  "
            f"{leg.get('market', '?')} -> {leg.get('selection', '?')}  "
            f"@ {float(leg.get('decimal_odds', 0.0)):.2f} ({leg.get('book', '?')})"
        )


def _prompt_result(bet_id: str) -> str | None:
    """Ask for the result; returns the outcome, None to skip, or 'QUIT'."""
    while True:
        raw = input(
            f"  Result for {bet_id}  [w=WIN / l=LOSS / v=VOID / Enter=skip / q=quit]: "
        ).strip().lower()
        if raw == "":
            return None
        if raw in ("q", "quit", "exit"):
            return "QUIT"
        outcome = _ANSWERS.get(raw)
        if outcome is not None:
            return outcome
        print("  !! Answer with w, l, v, Enter (skip) or q (quit).")


def main() -> None:
    _banner("SPORTY-EDGE  |  SETTLE PENDING BETS")
    ledger_path = None
    if "--ledger" in sys.argv:
        idx = sys.argv.index("--ledger")
        if idx + 1 < len(sys.argv):
            ledger_path = sys.argv[idx + 1]
    logger = BetLogger(path=ledger_path) if ledger_path else BetLogger()

    pending = logger.pending()
    if not pending:
        print("  Ledger has no PENDING bets — nothing to settle.")
        _print_report(logger)
        return

    print(f"  {len(pending)} pending bet(s) found in {logger.path}.")

    settled = 0
    for index, rec in enumerate(pending, start=1):
        _render_bet(rec, index, len(pending))
        answer = _prompt_result(rec["bet_id"])
        if answer == "QUIT":
            print("  Quitting — remaining bets stay PENDING.")
            break
        if answer is None:
            print(f"  .. skipped {rec['bet_id']} (still PENDING).")
            continue
        try:
            settled_rec = logger.settle(rec["bet_id"], answer)
        except (ValueError, KeyError) as exc:
            # The ledger changed underneath us or the row is malformed —
            # report it and move on rather than losing the whole session.
            print(f"  !! Could not settle {rec['bet_id']}: {exc}")
            continue
        settled += 1
        print(
            f"  -> {settled_rec['bet_id']} settled {answer}: "
            f"payout {settled_rec['payout_units']:.2f}u, "
            f"profit {settled_rec['profit_units']:+.2f}u"
        )

    _print_report(logger)


def _print_report(logger: BetLogger) -> None:
    """Metrics + per-slip-type breakdown after settlement."""
    _banner("LEDGER REPORT")
    metrics = logger.metrics()
    print(f"  Bets {metrics['total_bets']} "
          f"(settled {metrics['settled']}, pending {metrics['pending']}) | "
          f"Win rate {metrics['win_rate'] * 100:.1f}% | "
          f"Staked {metrics['total_staked_units']:.2f}u | "
          f"Profit {metrics['profit_units']:+.2f}u | "
          f"ROI {metrics['roi'] * 100:+.2f}%")

    breakdown = logger.breakdown(by="slip_type")
    if breakdown.empty:
        print("  No settled bets yet — breakdown appears after the first settle.")
        return
    print("\n  By slip type:")
    print(breakdown.to_string(index=False))


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n  Interrupted — nothing was lost; settled bets are saved.")
        sys.exit(130)
