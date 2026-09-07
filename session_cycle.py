"""
session_cycle.py (FINAL) - the 30-min always-on orchestrator.
Settle finished games -> top up smart picks to 8/session -> commit.
Exhausted lanes announce ONCE. Idempotent and dedupe-safe.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from utils.logger import BetLogger
from utils.session import clock_line, detect
from utils.term import force_utf8_stdio

STATE_PATH = Path("data") / "session_state.json"
TARGET = 8


def _pull():
    try:
        subprocess.run(["git", "pull", "--rebase", "-X", "theirs",
                        "origin", "main"],
                       capture_output=True, text=True, timeout=90)
    except Exception as exc:
        # Never silent: a skipped pull means we may be settling against a
        # stale ledger.  The run continues (offline-safe) but says so.
        print(f"[cycle] git pull skipped ({exc!r})")


def _run(script):
    try:
        subprocess.run([sys.executable, script], timeout=1500, check=False)
    except Exception as exc:
        print(f"[cycle] {script} failed: {exc!r}")


def _count_today(logger, session_name):
    # UTC date: internal standard across machines.  The old date.today()
    # (local, EAT+3) drifted from the UTC logged_at stamps after 21:00 EAT
    # and silently undercounted the session's picks.
    today = datetime.now(timezone.utc).date().isoformat()
    return sum(
        1 for rec in logger._read_all()
        if rec.get("session") == session_name
        and str(rec.get("logged_at", "")).startswith(today)
    )


def _load_state():
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
        if isinstance(s, dict) and s.get("date") == _utc_today():
            return s
    except (OSError, json.JSONDecodeError):
        pass
    return {"date": _utc_today(), "session": "",
            "announced": False, "exhausted": False}


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _save_state(s):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")


def main():
    force_utf8_stdio()
    _pull()
    session = detect()
    lg = BetLogger()
    state = _load_state()
    if state.get("session") != session.name:
        state = {"date": _utc_today(), "session": session.name,
                 "announced": False, "exhausted": False}
        _save_state(state)

    print(f"[cycle] {clock_line()}")
    _run("settle_auto.py")

    n = _count_today(lg, session.name)
    if n < TARGET and not state.get("exhausted"):
        before = n
        _run("smart_picks.py")
        n = _count_today(lg, session.name)
        if n == before and before > 0:
            state["exhausted"] = True
            from notify.telegram import TelegramNotifier
            t = TelegramNotifier()
            if t.is_configured:
                t.send(f"{session.emoji} {session.name}: market exhausted at "
                       f"{before}/8 - next session reopens the board.")
            _save_state(state)
    if n >= TARGET and not state.get("announced"):
        from notify.telegram import TelegramNotifier
        t = TelegramNotifier()
        if t.is_configured:
            t.send(f"✅ {session.emoji} {session.name} fully covered - "
                   f"{n} picks live across the platforms.")
        state["announced"] = True
        _save_state(state)
    print(f"[cycle] done | picks {n}/{TARGET}")


if __name__ == "__main__":
    main()