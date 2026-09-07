"""session_cycle.py (DUAL-LANE FINAL) - 30-min always-on orchestrator.
Settle finished games -> top up smart picks to 8 -> refresh history data
once per UTC day. Idempotent, dedupe-safe, quiet when covered."""

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

STATE_PATH = Path("data") / "session_state.json"
TARGET = 8


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _pull() -> None:
    try:
        subprocess.run(["git", "pull", "--rebase", "-X", "theirs",
                        "origin", "main"],
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
    return sum(
        1 for rec in logger._read_all()
        if rec.get("session") == session_name
        and str(rec.get("logged_at", ""))[:10] == today
    )


def _load_state() -> dict:
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
        if isinstance(s, dict) and s.get("utc_date") == _utc_today():
            return s
    except (OSError, json.JSONDecodeError):
        pass
    return {"utc_date": _utc_today(), "session": "",
            "announced": False, "exhausted": False, "history_done": False}


def _save_state(s: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")


def main() -> None:
    _pull()
    session = detect()
    lg = BetLogger()
    state = _load_state()
    if state.get("session") != session.name:
        state = {"utc_date": _utc_today(), "session": session.name,
                 "announced": False, "exhausted": False,
                 "history_done": state.get("history_done", False)}
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
                       f"{before}/8 - next window reopens the board.")
            _save_state(state)
    if n >= TARGET and not state.get("announced"):
        from notify.telegram import TelegramNotifier
        t = TelegramNotifier()
        if t.is_configured:
            t.send(f"✅ {session.emoji} {session.name} fully covered - "
                   f"{n} picks live across the platforms.")
        state["announced"] = True
        _save_state(state)

    if not state.get("history_done"):
        print("[cycle] refreshing history data (once per day)...")
        _run("update_history.py")
        state["history_done"] = True
        _save_state(state)

    print(f"[cycle] done | picks {n}/{TARGET}")

    # daily heartbeat so silence is never ambiguous
    hb = Path("data") / "heartbeat.txt"
    today = datetime.now(timezone.utc).date().isoformat()
    last = ""
    try:
        last = hb.read_text().strip()
    except OSError:
        pass
    if last != today:
        from notify.telegram import TelegramNotifier
        tg = TelegramNotifier()
        if tg.is_configured:
            tg.send(f"heartbeat: cycles running, today covered={n}/{TARGET}")
        hb.write_text(today)


if __name__ == "__main__":
    main()