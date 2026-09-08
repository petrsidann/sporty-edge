"""session_cycle.py - 30-min orchestrator (Concentrated Value spec).
Per-session targets 5/5/8/5 — min = cap = session.target (no surge to 25).
Runs settle_auto + smart_picks, then sends ONE Telegram summary per cycle
that always includes: picks added this cycle, session progress (n/target),
and credits (last API response + daily key-audit pool). Heartbeat every
cycle; one Telegram heartbeat per UTC day."""
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
CREDITS_LAST_PATH = Path("data") / "credits_last.json"
CREDITS_SUMMARY_PATH = Path("data") / "credits_summary.json"


def _credits_report() -> tuple[object, tuple[int, int] | None]:
    """(credits, pool) for the cycle summary — Phase 2.

    credits: get_last_credits() if THIS process called the feed (it does
    not — smart_picks runs as a subprocess), else data/credits_last.json
    written by smart_picks this cycle, else '?' (honest unknown).
    pool: (alive, total_credits) from data/credits_summary.json — written
    once per day by key_audit.py chained into update_history.py.
    """
    from feeds.oddsapi import get_last_credits
    credits: object = get_last_credits()
    if credits is None:
        try:
            d = json.loads(CREDITS_LAST_PATH.read_text(encoding="utf-8-sig"))
            credits = int(d["credits"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError,
                ValueError):
            credits = "?"
    pool: tuple[int, int] | None = None
    try:
        d = json.loads(CREDITS_SUMMARY_PATH.read_text(encoding="utf-8-sig"))
        pool = (int(d["alive"]), int(d["total_credits"]))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        pool = None
    return credits, pool


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _pull() -> None:
    try:
        subprocess.run(["git", "pull", "--rebase", "-X", "theirs", "origin", "main"],
                       capture_output=True, text=True, timeout=90)
    except Exception as exc:
        print(f"[cycle] git pull skipped ({type(exc).__name__})")


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
    return {"utc_date": _utc_today(), "session": ""}


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
        # Keep the once-per-day flags (history refresh, calibration ping)
        # across session boundaries — they are UTC-day-scoped, not session-
        # scoped, and re-running them would waste API credits.
        state = {"utc_date": _utc_today(), "session": session.name,
                 "history_date": state.get("history_date", ""),
                 "calibration_date": state.get("calibration_date", "")}
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

    # Phase 1b: min = cap = session.target (5/5/8/5). No surge to 25.
    target = session.target
    n = _count_today(lg, session.name)
    picks_before = n
    errors = 0
    if n < target:
        _run("smart_picks.py")
        n = _count_today(lg, session.name)
    picks_added = max(0, n - picks_before)

    # Phase 2b: ONE summary per cycle, always with picks added, progress
    # and credits (+ daily pool total when the key audit has run).
    from notify.telegram import TelegramNotifier
    tg = TelegramNotifier()
    credits, pool = _credits_report()
    summary = (f"{'✅' if n >= target else '📋'} {session.emoji} {session.name}: "
               f"+{picks_added} picks this cycle | progress {n}/{target} | "
               f"credits ~{credits}")
    if pool is not None:
        summary += f" | pool ~{pool[1]} across {pool[0]} keys"
    print(f"[cycle] {summary}")
    if tg.is_configured:
        tg.send(summary)

    # Heartbeat: every cycle appends to heartbeat.log; one Telegram ping per UTC day.
    hb = Heartbeat()
    hb.tick(session_name=session.name, picks_so_far=n, errors=errors)
    hb.maybe_telegram(tg)

    print(f"[cycle] done | picks {n} (min {target}, cap {target})")


if __name__ == "__main__":
    main()