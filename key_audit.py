"""key_audit.py - tests every key, shows credits left on each."""
import json, urllib.request
from pathlib import Path

data = json.loads(Path("data/credentials.json").read_text(encoding="utf-8-sig"))
keys = [str(data.get("odds_api_key") or "").strip()]
keys += [str(k).strip() for k in (data.get("odds_api_keys_extra") or [])]
keys = [k for k in keys if k]

print(f"  {len(keys)} keys found:\n")
alive = 0
total_credits = 0
last_remaining = None
for i, k in enumerate(keys, start=1):
    tail = k[-4:]
    try:
        url = f"https://api.the-odds-api.com/v4/sports?apiKey={k}"
        with urllib.request.urlopen(url, timeout=15) as r:
            remaining = r.headers.get("x-requests-remaining", "?")
            print(f"  key{i} (..{tail}): ALIVE   credits ~{remaining}")
            alive += 1
            try:
                total_credits += int(float(remaining))
                last_remaining = int(float(remaining))
            except ValueError:
                pass
    except Exception as exc:
        code = getattr(exc, "code", "?")
        print(f"  key{i} (..{tail}): DEAD (HTTP {code})")

print(f"\n  ALIVE: {alive}/{len(keys)} | total credits across pool: ~{total_credits}")
print("  Replace dead keys in data/credentials.json (setup_credentials.py)")

# Phase 2c: persist the pool snapshot so session_cycle's per-cycle Telegram
# summary can include the pool total.  Written once per day via
# update_history.py (the daily refresh chains this audit).
from datetime import datetime, timezone  # noqa: E402

snapshot = {
    "alive": alive,
    "total_credits": total_credits,
    "keys": len(keys),
    "checked_at": datetime.now(timezone.utc).isoformat(),
}
try:
    Path("data/credits_summary.json").write_text(
        json.dumps(snapshot, indent=2), encoding="utf-8")
    print("  wrote data/credits_summary.json")
except OSError as exc:
    print(f"  !! could not write data/credits_summary.json: {exc!r}")

# Also refresh the "last-seen credits" file (the same contract the feed
# writes after every API call) so the daily cycle summary shows a real
# number even before the next feed fetch.
if last_remaining is not None:
    try:
        Path("data/credits_last.json").write_text(
            json.dumps({"credits": last_remaining,
                        "ts": datetime.now(timezone.utc).isoformat()},
                       indent=2), encoding="utf-8")
        print(f"  wrote data/credits_last.json (credits ~{last_remaining})")
    except OSError as exc:
        print(f"  !! could not write data/credits_last.json: {exc!r}")
