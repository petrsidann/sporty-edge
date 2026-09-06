"""
settle_auto.py - non-interactive settlement (for cron + humans in a rush).

    python settle_auto.py

Pulls the ledger from GitHub, fetches real final scores, auto-settles every
slip it can judge (moneyline / 1X2 / totals).  Ambiguous slips stay PENDING
for python settle.py (interactive).
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from datetime import datetime, timezone

from config.settings import FEED_SETTINGS


def _pull():
    try:
        r = subprocess.run(["git", "pull", "--rebase", "-X", "theirs",
                            "origin", "main"],
                           capture_output=True, text=True, timeout=60)
        print("  git pull ok")
    except Exception as exc:
        print(f"  git pull skipped ({type(exc).__name__})")


def _keys() -> list[str]:
    return [k for k in [FEED_SETTINGS.odds_api_key.strip(),
                        *[k.strip() for k in FEED_SETTINGS.api_keys]] if k]


def _get(url: str):
    for i, k in enumerate(_keys()):
        try:
            with urllib.request.urlopen(url.replace("APIKEY", k), timeout=15) as r:
                return json.loads(r.read().decode()), None
        except urllib.error.HTTPError as e:
            if e.code in (401, 429) and i + 1 < len(_keys()):
                continue
            return None, f"HTTP {e.code}"
        except Exception as exc:
            return None, repr(exc)
    return None, "no keys"


def main() -> None:
    print("=" * 60)
    print("  settle_auto - final scores -> automatic settlement")
    print("=" * 60)
    _pull()

    from utils.logger import BetLogger
    lg = BetLogger()
    pending = lg.pending()
    if not pending:
        print("  Nothing pending.")
        return

    titles = {str(l.get("league") or "").strip()
              for r in pending for l in r.get("legs", [])} - {""}
    smap: dict[str, dict] = {}
    data, err = _get("https://api.the-odds-api.com/v4/sports?apiKey=APIKEY")
    if data:
        key_by_title = {s.get("title", ""): s["key"] for s in data}
        for title in titles:
            skey = key_by_title.get(title)
            if not skey:
                continue
            events, _ = _get(
                f"https://api.the-odds-api.com/v4/sports/{skey}/scores"
                f"?daysFrom=3&apiKey=APIKEY")
            if not events:
                continue
            for ev in events:
                if not ev.get("completed") or not ev.get("scores"):
                    continue
                hs = as_ = None
                for s in ev["scores"]:
                    if s.get("name") == ev.get("home_team"):
                        hs = s.get("score")
                    elif s.get("name") == ev.get("away_team"):
                        as_ = s.get("score")
                if hs is None or as_ is None:
                    continue
                try:
                    smap[str(ev["id"])[:10].upper()] = {
                        "home": int(hs), "away": int(as_)}
                except (TypeError, ValueError):
                    continue

    auto = 0
    for rec in pending:
        results = []
        for leg in rec.get("legs", []):
            sc = smap.get(str(leg.get("match_id") or "").upper())
            if sc is None:
                results.append(None)
                continue
            m = str(leg.get("market", "")).upper()
            sel = str(leg.get("selection", ""))
            h, a = sc["home"], sc["away"]
            if m.startswith(("ML", "MONEYLINE", "1X2")):
                if sel == "Home":
                    results.append("WIN" if h > a else "LOSS")
                elif sel == "Away":
                    results.append("WIN" if a > h else "LOSS")
                elif sel == "Draw":
                    results.append("WIN" if h == a else "LOSS")
                else:
                    results.append(None)
            elif m.startswith("O/U"):
                try:
                    line = float(m.split()[-1])
                    total = h + a
                    results.append(
                        ("WIN" if total > line else "LOSS")
                        if sel == "Over" else
                        ("WIN" if total < line else "LOSS")
                        if sel == "Under" else None)
                except (ValueError, IndexError):
                    results.append(None)
            else:
                results.append(None)

        if all(r == "WIN" for r in results):
            lg.settle(rec["bet_id"], "WIN"); auto += 1
            print(f"  {rec['bet_id']} -> WIN (auto)")
        elif any(r == "LOSS" for r in results):
            lg.settle(rec["bet_id"], "LOSS"); auto += 1
            print(f"  {rec['bet_id']} -> LOSS (auto)")

    m = lg.metrics()
    print(f"\n  auto-settled {auto} | metrics: {m}")
    print("  Ambiguous slips (spreads etc.) stay pending for python settle.py")
    print('  Sync: git add data/bets.jsonl ; git commit -m "Auto settle" '
          '; git pull --rebase -X theirs origin main ; git push')


if __name__ == "__main__":
    main()