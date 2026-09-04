"""
settle.py v2 - interactive settlement, with ledger sync and smart ordering.

    python settle.py

Pulls the latest ledger from GitHub first (cloud-run bets live there),
splits pending bets into "likely finished" (logged > 6h ago) and "recent",
then asks WIN / LOSS / VOID for each.  The aggression tiers read SETTLED
results only - this is how the system learns.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from utils.logger import BetLogger

_VALID = {"w": "WIN", "l": "LOSS", "v": "VOID"}


def _pull() -> None:
    """Sync the ledger from GitHub (cloud runs commit results there)."""
    try:
        r = subprocess.run(
            ["git", "pull", "--rebase", "-X", "theirs", "origin", "main"],
            capture_output=True, text=True, timeout=60,
        )
        out = (r.stdout or "") + (r.stderr or "")
        tail = out.strip().splitlines()[-1] if out.strip() else "no output"
        print(f"  git pull: {tail}")
    except Exception as exc:
        print(f"  git pull skipped ({exc.__class__.__name__}) - using local ledger.")


def _age_hours(logged_at: str) -> float:
    try:
        dt = datetime.fromisoformat(logged_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return 0.0


def _ask(rec: dict, lg: BetLogger) -> None:
    print("-" * 64)
    print(f"  {rec['bet_id']}  [{rec.get('slip_type')}]  session={rec.get('session')}")
    for i, leg in enumerate(rec.get("legs", []), start=1):
        print(f"    {i}. {leg.get('match_label', '?')} | "
              f"{leg.get('market')} -> {leg.get('selection')} @ {leg.get('decimal_odds')}")
    print(f"    odds {rec.get('combined_odds')} | stake {rec.get('stake_units')}u")
    while True:
        ans = input("    result? [W]in / [L]oss / [V]oid / [s]kip: ").strip().lower()
        if ans in ("s", ""):
            print("    skipped.")
            return
        outcome = _VALID.get(ans)
        if outcome is None:
            print("    enter W, L, V or s.")
            continue
        try:
            lg.settle(rec["bet_id"], outcome)
            print(f"    OK {rec['bet_id']} settled {outcome}.")
        except ValueError as exc:
            print(f"    ! {exc}")
        return


def main() -> None:
    print("=" * 64)
    print("  settle.py - settle your bets, build your record")
    print("=" * 64)
    _pull()

    lg = BetLogger()
    pending = lg.pending()
    if not pending:
        print("  Nothing pending.")
        return

    finished = [r for r in pending if _age_hours(r.get("logged_at", "")) > 6.0]
    recent = [r for r in pending if r not in finished]

    if finished:
        print(f"\n  LIKELY FINISHED ({len(finished)}) - settle these first:")
        for rec in finished:
            _ask(rec, lg)
    if recent:
        print(f"\n  RECENT ({len(recent)}) - games not played yet; skip unless void:")
        for rec in recent:
            _ask(rec, lg)

    print("=" * 64)
    m = lg.metrics()
    print("  Metrics:", m)
    settled = int(m.get("settled", 0))
    if settled < 25:
        print(f"  {settled}/25 settled toward the first tier promotion.")
    else:
        print("  Tier gate reached - stakes scale on the next run.")
    print("  Sync now:")
    print('    git add data/bets.jsonl ; git commit -m "Settle results" '
          '; git pull --rebase -X theirs origin main ; git push')


if __name__ == "__main__":
    main()