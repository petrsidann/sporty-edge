"""discover_sports.py - fetch ALL valid sport keys (1 credit), save, report."""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

from config.settings import FEED_SETTINGS

OUT = Path("data") / "valid_sports.json"


def _keys() -> list[str]:
    keys = [os.environ.get("ODDS_API_KEY", "").strip()]
    keys.append(FEED_SETTINGS.odds_api_key.strip())
    keys += [k.strip() for k in FEED_SETTINGS.api_keys]
    seen, out = set(), []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def main() -> None:
    for i, key in enumerate(_keys(), start=1):
        try:
            req = urllib.request.Request(
                f"https://api.the-odds-api.com/v4/sports?apiKey={key}",
                headers={"User-Agent": "sporty-edge/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                sports = json.loads(r.read().decode("utf-8"))
            remaining = r.headers.get("x-requests-remaining", "?")
            OUT.parent.mkdir(parents=True, exist_ok=True)
            OUT.write_text(json.dumps(sports), encoding="utf-8")
            print(f"  key{i} (..{key[-4:]}): OK | {len(sports)} valid sports "
                  f"| credits left ~{remaining}")
            return
        except Exception as exc:
            code = getattr(exc, "code", "?")
            print(f"  key{i} (..{key[-4:]}): failed (HTTP {code})")
    print("  ALL KEYS FAILED - check credits")


if __name__ == "__main__":
    main()