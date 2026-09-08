"""session_cycle.py - 30-min orchestrator. Min 10 picks, up to 25 when the
board is fat. Sends a Telegram status EVERY cycle - silence is impossible."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from utils.term import force_utf8_stdio
    force_utf8_stdio()
except Exception:
    pass

from utils.logger import BetLogger
from utils.session import clock_line, detect
from utils.heartbeat import Heartbeat

STATE_PATH = Path("data") / "session_state.json"
TARGET_MIN = 10
TARGET_MAX = 25


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _pull() -> None:
    try:
        subprocess.run(["git", "pull", "--rebase", "-X", "theirs", "origin", "main"],
                       capture_output=True, text=True, timeout=90)
    except Exception:
        pass


def _run(script: str) -> None:
    try:
        subprocess.run([sys.executable, script], timeout=1500, check=False)
    except Exception as exc:
        print(f"[cycle] {script} failed: {exc!r}")


def _count_today(logger: BetLogger, session_name: str) -> int:
    today = _utc_today()
    return sum(1 for rec in logger._read_all()
               if rec.get("session") == session_name
               and str(rec.get("logged_at", ""))[:10] == today)


def _load_state() -> dict:
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
        if isinstance(s, dict) and s.get("utc_date") == _utc_today():
            return s
    except (OSError, json.JSONDecodeError):
        pass
    return {"utc_date": _utc_today(), "session": "", "covered_ping": ""}


def _save_state(s: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")


def _daily_refresh(state: dict) -> None:
    """Run history refresh once per UTC day. Flag persisted in session_state.json
    so it survives across cycles and processes."""
    if state.get("history_date") == _utc_today():
        return
    print("[cycle] daily history refresh (fetch_history + fetch_more_history)...")
    _run("fetch_history.py")
    _run("fetch_more_history.py")
    state["history_date"] = _utc_today()
    _save_state(state)
    print("[cycle] daily history refresh complete.")


def main() -> None:
    _pull()
    session = detect()
    lg = BetLogger()
    state = _load_state()
    if state.get("session") != session.name:
        state = {"utc_date": _utc_today(), "session": session.name,
                 "covered_ping": ""}
        _save_state(state)

    # Daily data refresh (once per UTC day, flag in session_state.json).
    _daily_refresh(state)

    # Phase 4c: on the 1st cycle of each UTC day, print and Telegram the
    # calibration one-liner for the active 1.55-2.60 band.
    if state.get("calibration_date") != _utc_today():
        from utils.calibration import calibration_one_liner
        _cal_line = calibration_one_liner(lg._read_all())
        print(f"[cycle] {_cal_line}")
        from notify.telegram import TelegramNotifier as _TgCal
        _tg_cal = _TgCal()
        if _tg_cal.is_configured:
            _tg_cal.send(f"📊 Daily calibration: {_cal_line}")
        state["calibration_date"] = _utc_today()
        _save_state(state)

    print(f"[cycle] {clock_line()}")
    _run("settle_auto.py")

    n = _count_today(lg, session.name)
    errors = 0
    if n < TARGET_MAX:
        _run("smart_picks.py")
        n = _count_today(lg, session.name)

    from notify.telegram import TelegramNotifier
    tg = TelegramNotifier()
    if tg.is_configured:
        if n >= TARGET_MIN and state.get("covered_ping") != session.name:
            tg.send(f"✅ {session.emoji} {session.name} covered - {n} picks live "
                    f"(target met, board keeps topping up to {TARGET_MAX}).")
            state["covered_ping"] = session.name
            _save_state(state)
        elif n < TARGET_MIN:
            tg.send(f"📋 {session.emoji} {session.name}: {n}/{TARGET_MIN} picks so "
                    f"far - next 30-min cycle adds more as games get listed.")
        else:
            tg.send(f"📋 {session.emoji} {session.name}: {n} picks live "
                    f"(max {TARGET_MAX}). Settles arrive automatically.")

    # Heartbeat: every cycle appends to heartbeat.log; one Telegram ping per UTC day.
    hb = Heartbeat()
    hb.tick(session_name=session.name, picks_so_far=n, errors=errors)
    hb.maybe_telegram(tg)

    print(f"[cycle] done | picks {n} (min {TARGET_MIN}, max {TARGET_MAX})")


if __name__ == "__main__":
    main()