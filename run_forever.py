"""
run_forever.py — scheduled runner for sporty-edge.

Runs the full pipeline (main.py) every RUN_INTERVAL_HOURS, forever, inside
a tmux session.  A crash in any single run never kills the loop.

Usage (inside tmux):
    python3 run_forever.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timezone

RUN_INTERVAL_HOURS: float = 6.0  # one full pipeline run every 6 hours


def _now() -> str:
    """Timestamped prefix for loop logs."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def main() -> None:
    """Run main.py on a schedule until stopped."""
    interval_seconds = int(RUN_INTERVAL_HOURS * 3600)
    print(f"[{_now()}] run_forever: starting — pipeline every {RUN_INTERVAL_HOURS}h")
    print(f"[{_now()}] run_forever: detach with Ctrl+B then D. Reattach: tmux attach -t edge")

    while True:
        print(f"[{_now()}] === pipeline run starting ===")
        try:
            result = subprocess.run([sys.executable, "main.py"], check=False)
            print(
                f"[{_now()}] === pipeline run finished "
                f"(exit code {result.returncode}) ==="
            )
        except Exception as exc:  # the loop must survive anything
            print(f"[{_now()}] pipeline crashed, loop continues: {exc!r}")

        print(f"[{_now()}] sleeping {RUN_INTERVAL_HOURS}h ...")
        try:
            time.sleep(interval_seconds)
        except KeyboardInterrupt:
            print(f"[{_now()}] run_forever stopped by user.")
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[run_forever] stopped.")