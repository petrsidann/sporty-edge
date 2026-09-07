"""watchdog.py - run after any silent hour: tells you exactly what's wrong.

    python watchdog.py
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from notify.telegram import TelegramNotifier
from utils.logger import BetLogger

log = Path("data") / "scheduler.log"


def tail(path: Path, n: int = 8) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]


def main() -> None:
    print("=" * 60)
    print("  WATCHDOG")
    print("=" * 60)

    # 1. ledger alive?
    lg = BetLogger()
    m = lg.metrics()
    print(f"  ledger: {m}")

    # 2. scheduler firing?
    lines = tail(log)
    recent = [ln for ln in lines if ln.strip()]
    print(f"  scheduler log lines (last {len(recent)}):")
    for ln in recent:
        print(f"    {ln[:100]}")

    # 3. telegram reachable?
    tg = TelegramNotifier()
    if tg.is_configured:
        ok = tg.send("watchdog ping - if you see this, delivery is alive")
        print(f"  telegram: {'OK' if ok else 'FAILED - check token/chat id'}")
    else:
        print("  telegram: NOT CONFIGURED")

    # 4. last scheduled-run heartbeat
    hb = Path("data") / "last_cycle.txt"
    if hb.exists():
        print(f"  last cycle marker: {hb.read_text().strip()}")
    else:
        print("  last cycle marker: none yet")


if __name__ == "__main__":
    main()