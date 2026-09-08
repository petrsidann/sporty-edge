"""
run_now.py - THE one command. Run this any time you want bets:

    python run_now.py

It does everything in order:
    1. Settles every finished game (real final scores, automatic).
    2. Scans all 37 leagues, session-bound (only games that finish today).
    3. Smart lanes: clear favorite -> pick the winner.
       Tight/draw-risky match -> pivots to the totals (goals) market,
       exactly as you asked: instead of a coin-flip 1X2, take Over/Under.
    4. Sends every pick to Telegram with team name, kickoff, settle time,
       price floor, and stake.
Run it again 2-3 hours later - it only adds NEW picks, never duplicates.
"""

from __future__ import annotations

import subprocess
import sys

from utils.term import force_utf8_stdio


def _run(script: str) -> None:
    """Run one pipeline script; failures never stop the chain."""
    print(f"\n>>> {script}")
    try:
        subprocess.run([sys.executable, script], timeout=1500, check=False)
    except Exception as exc:
        print(f"    !! {script} failed: {exc!r} - continuing.")


def main() -> None:
    force_utf8_stdio()
    print("=" * 66)
    print("  sporty-edge | run_now: settle -> scan -> Telegram picks")
    print("=" * 66)

    _run("settle_auto.py")      # 1. score what finished
    _run("board.py")      # 2. winner-lane + totals-lane picks -> Telegram

    print("\n" + "=" * 66)
    print("  DONE. Place what clears the price floors (click TEAM NAMES).")
    print("  After the games finish:  python run_now.py   (settles + next picks)")
    print("=" * 66)


if __name__ == "__main__":
    main()