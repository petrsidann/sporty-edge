"""update_history.py - refresh ALL history data (idempotent, safe daily)."""
from __future__ import annotations

import subprocess
import sys


def _run(script: str) -> None:
    print(f"\n=== {script} ===")
    try:
        subprocess.run([sys.executable, script], timeout=900, check=False)
    except Exception as exc:
        print(f"  !! {script} failed: {exc!r}")


if __name__ == "__main__":
    _run("fetch_history.py")        # soccer: 6 leagues, 3 seasons
    _run("fetch_more_history.py")   # tennis, NBA, NFL, MLB
    _run("key_audit.py")            # Phase 2c: write data/credits_summary.json
    print("\nHistory refresh complete. Workflow commits data/ automatically.")