"""key_audit.py - tests every key, shows credits left on each.
Phase 2c: also writes data/credits_summary.json with {alive, total_credits}
so session_cycle can include the pool total in its Telegram summary."""
import json
import urllib.request
from pathlib import Path

data = json.loads(Path("data/credentials.json").read_text(encoding="utf-8-sig"))
keys = [str(data.get("odds_api_key") or "").strip()]
keys += [str(k).strip() for k in (data.get("odds_api_keys_extra") or [])]
keys = [k for k in keys if k]

print(f"  {len(keys)} keys found:\n")
alive = 0
total_credits = 0
for i, k in enumerate(keys, start=1):
    tail = k[-4:]
    try:
        url = f"https://api.the-odds-api.com/v4/sports?apiKey={k}"
        with urllib.request.urlopen(url, timeout=15) as r:
            remaining = r.headers.get("x-requests-remaining", "?")
            print(f"  key{i} (..{tail}): ALIVE   credits ~{remaining}")
            alive += 1
            try: total_credits += int(float(remaining))
            except ValueError: pass
    except Exception as exc:
        code = getattr(exc, "code", "?")
        print(f"  key{i} (..{tail}): DEAD (HTTP {code})")

print(f"\n  ALIVE: {alive}/{len(keys)} | total credits across pool: ~{total_credits}")
print("  Replace dead keys in data/credentials.json (setup_credentials.py)")

# Phase 2c: persist credits summary for session_cycle reporting.
summary = {"alive": alive, "total_credits": total_credits}
out = Path("data") / "credits_summary.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
print(f"  wrote {out}: {summary}")
