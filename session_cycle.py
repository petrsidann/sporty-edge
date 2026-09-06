"""
session_cycle.py (FINAL) - the 30-min always-on orchestrator.
Idempotent: settles finished games, tops up singles/totals per session,
announces coverage once, marks exhausted lanes once. Safe to run forever.
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
TARGET_TOTALS = 8


def _pull():
    try:
        subprocess.run(["git", "pull", "--rebase", "-X", "theirs", "origin", "main"],
                       capture_output=True, text=True, timeout=90)
    except Exception:
        pass


def _run(script):
    try:
        subprocess.run([sys.executable, script], timeout=1500, check=False)
    except Exception as exc:
        print(f"[cycle] {script} failed: {exc!r}")


def _count_today(logger, session_name, ou_only=False):
    today = date.today().isoformat()
    n = 0
    for rec in logger._read_all():
        if rec.get("session") != session_name: continue
        if not str(rec.get("logged_at", "")).startswith(today): continue
        if ou_only:
            if any(str(l.get("market", "")).upper().startswith("O/U")
                   for l in rec.get("legs", [])): n += 1
        else:
            n += 1
    return n


def _load_state():
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
        if isinstance(s, dict) and s.get("date") == date.today().isoformat():
            return s
    except (OSError, json.JSONDecodeError):
        pass
    return {"date": date.today().isoformat(), "session": "",
            "singles_announced": False, "singles_exhausted": False,
            "totals_exhausted": False}


def _save_state(s):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")


def main():
    _pull()
    session = detect()
    lg = BetLogger()
    state = _load_state()
    if state.get("session") != session.name:
        state = {"date": date.today().isoformat(), "session": session.name,
                 "singles_announced": False, "singles_exhausted": False,
                 "totals_exhausted": False}
        _save_state(state)

    print(f"[cycle] {clock_line()}")

    _run("settle_auto.py")

    singles = _count_today(lg, session.name)
    if singles < TARGET_SINGLES and not state.get("singles_exhausted"):
        before = singles
        _run("single_shot.py")
        singles = _count_today(lg, session.name)
        if singles == before:
            state["singles_exhausted"] = True
            from notify.telegram import TelegramNotifier
            tgn = TelegramNotifier()
            if tgn.is_configured and before > 0:
                tgn.send(f"{session.emoji} {session.name}: singles complete at {before}/8 - world supply reached.")
            _save_state(state)
    if singles >= TARGET_SINGLES and not state.get("singles_announced"):
        from notify.telegram import TelegramNotifier
        tgn = TelegramNotifier()
        if tgn.is_configured:
            tgn.send(f"✅ {session.emoji} {session.name} fully covered - {singles} picks live. Settlements arrive automatically.")
        state["singles_announced"] = True
        _save_state(state)

    totals_n = _count_today(lg, session.name, ou_only=True)
    if totals_n < TARGET_TOTALS and not state.get("totals_exhausted"):
        before = totals_n
        _run("totals.py")
        totals_n = _count_today(lg, session.name, ou_only=True)
        if totals_n == before:
            state["totals_exhausted"] = True
            _save_state(state)

    print(f"[cycle] done | singles {singles}/8 | totals {totals_n}/8")


if __name__ == "__main__":
    main()