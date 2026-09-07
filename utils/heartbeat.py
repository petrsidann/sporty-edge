"""
heartbeat.py — cycle-level telemetry so silence is always diagnosable.

Every session_cycle run appends one line to data/heartbeat.log:
    UTC timestamp | session | picks-so-far | errors

One Telegram heartbeat is sent per UTC day (first cycle that detects
the day rotated).  If the owner sees no heartbeat line for >60 min
while a session is active, the pipeline is stuck — check scheduler.log.

    from utils.heartbeat import Heartbeat
    hb = Heartbeat()
    hb.tick(session_name="S2", picks_so_far=12, errors=0)
    hb.maybe_telegram(tg)   # sends once per UTC day
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from notify.telegram import TelegramNotifier

LOG_PATH = Path("data") / "heartbeat.log"
_STATE_PATH = Path("data") / "session_state.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_today() -> str:
    return _utc_now().date().isoformat()


class Heartbeat:
    """Append-only heartbeat ledger + once-per-day Telegram ping."""

    def __init__(self, log_path: Path = LOG_PATH) -> None:
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def tick(self, session_name: str, picks_so_far: int, errors: int = 0) -> None:
        """Append one heartbeat line (called every cycle)."""
        line = {
            "utc": _utc_now().isoformat(timespec="seconds"),
            "session": session_name,
            "picks": picks_so_far,
            "errors": errors,
        }
        try:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def maybe_telegram(self, tg: TelegramNotifier) -> None:
        """Send ONE Telegram heartbeat per UTC day."""
        if not tg.is_configured:
            return
        state: dict = {}
        try:
            s = json.loads(_STATE_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(s, dict):
                state = s
        except (OSError, json.JSONDecodeError):
            pass
        if state.get("heartbeat_date") == _utc_today():
            return
        # Count today's heartbeats for the status line.
        n_heartbeats = 0
        try:
            for line in self.log_path.read_text(encoding="utf-8-sig").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(rec.get("utc", ""))[:10] == _utc_today():
                    n_heartbeats += 1
        except OSError:
            pass
        msg = (f"💓 heartbeat {_utc_now().strftime('%H:%M UTC')} | "
               f"{n_heartbeats} cycles today | pipeline alive.")
        if tg.send(msg):
            state["heartbeat_date"] = _utc_today()
            try:
                _STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
            except OSError:
                pass
