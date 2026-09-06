"""
session_cycle.py - the ALWAYS-ON orchestrator. Runs every 30 min from
GitHub Actions (also fine locally: python session_cycle.py).

Idempotent per cycle:
    1. Pull latest ledger (cloud <-> laptop sync).
    2. Auto-settle every bet whose game finished (real final scores).
    3. Count singles logged THIS session (today + session name).
         < 8 -> run single_shot.py (dedupe adds only NEW picks)
         = 8 -> covered; announce once, then quiet for this session
    4. Same top-up logic for the totals lane (target 4).
    5. If a scan adds zero new picks, mark that lane exhausted for the
       session and stop re-scanning it (no spam, honest shortfall once).
    6. Commit ledger + cache + state back to the repo.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from utils.logger import BetLogger
from utils.session import clock_line, detect

STATE_PATH = Path("data") / "session_state.json"
TARGET_SINGLES = 8
TARGET_TOTALS = 4


def _pull() -> None:
    try:
        subprocess.run(
            ["git", "pull", "--rebase", "-X", "theirs", "origin", "main"],
            capture_output=True, text=True, timeout=90,
        )
    except Exception:
        pass


def _run(script: str) -> None:
    """Run a pipeline script as a subprocess; never crash the cycle."""
    try:
        subprocess.run([sys.executable, script], timeout=1500, check=False)
    except Exception as exc:
        print(f"[cycle] {script} failed: {exc!r}")


def _count_today(logger: BetLogger, session_name: str, ou_only: bool = False) -> int:
    """Bets logged today for this session (totals lane = legs starting O/U)."""
    today = date.today().isoformat()
    n = 0
    for rec in logger._read_all():
        if rec.get("session") != session_name:
            continue
        if not str(rec.get("logged_at", "")).startswith(today):
            continue
        if ou_only:
            if any(str(l.get("market", "")).upper().startswith("O/U")
                   for l in rec.get("legs", [])):
                n += 1
        else:
            n += 1
    return n


def _load_state() -> dict:
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
        if isinstance(s, dict) and s.get("date") == date.today().isoformat():
            return s
    except (OSError, json.JSONDecodeError):
        pass
    return {"date": date.today().isoformat(), "session": "",
            "singles_announced": False, "singles_exhausted": False,
            "totals_exhausted": False}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main() -> None:
    _pull()
    session = detect()
    lg = BetLogger()
    state = _load_state()

    # new session (or new day) -> fresh state
    if state.get("session") != session.name:
        state = {"date": date.today().isoformat(), "session": session.name,
                 "singles_announced": False, "singles_exhausted": False,
                 "totals_exhausted": False}
        _save_state(state)

    print(f"[cycle] {clock_line()}")

    # 1) settle whatever finished
    _run("settle_auto.py")

    # 2) singles top-up
    singles = _count_today(lg, session.name)
    if singles < TARGET_SINGLES and not state.get("singles_exhausted"):
        before = singles
        print(f"[cycle] singles {before}/{TARGET_SINGLES} - scanning...")
        _run("single_shot.py")
        singles = _count_today(lg, session.name)
        if singles == before and before >= 0:
            # a full scan added nothing new -> lane exhausted this session
            state["singles_exhausted"] = True
            from notify.telegram import TelegramNotifier
            tg = TelegramNotifier()
            if tg.is_configured and before > 0:
                tg.send(f"{session.emoji} {session.name}: singles lane "
                        f"exhausted at {before}/8 - world supply reached. "
                        f"Next session reopens the board.")
            _save_state(state)
    if singles >= TARGET_SINGLES and not state.get("singles_announced"):
        from notify.telegram import TelegramNotifier
        tg = TelegramNotifier()
        if tg.is_configured:
            tg.send(f"✅ {session.emoji} {session.name} fully covered - "
                    f"{singles} picks across the platforms. Settlements "
                    f"arrive automatically as games end.")
        state["singles_announced"] = True
        _save_state(state)

    # 3) totals top-up
    totals_n = _count_today(lg, session.name, ou_only=True)
    if totals_n < TARGET_TOTALS and not state.get("totals_exhausted"):
        before = totals_n
        _run("totals.py")
        totals_n = _count_today(lg, session.name, ou_only=True)
        if totals_n == before:
            state["totals_exhausted"] = True
            _save_state(state)

    print(f"[cycle] done | singles {singles}/{TARGET_SINGLES} | "
          f"totals {totals_n}/{TARGET_TOTALS}")


if __name__ == "__main__":
    main()