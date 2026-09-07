"""
settle_auto.py v3 - auto-settlement with full progress output and hard caps.

    python settle_auto.py

* Prints every step (never silent longer than one request).
* Caps score fetches at 12 leagues per cycle (rest next cycle).
* Skips score fetching entirely when the /sports call fails.
* Auto-settles ML/1X2/O/U/DC legs from real final scores.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone

try:
    from utils.term import force_utf8_stdio
    force_utf8_stdio()
except Exception:
    pass

from config.settings import FEED_SETTINGS

MAX_LEAGUE_FETCHES = 12
_VALID = {"WIN", "LOSS"}


def _pull() -> None:
    print("  [1/4] syncing ledger from GitHub...")
    try:
        subprocess.run(["git", "pull", "--rebase", "-X", "theirs",
                        "origin", "main"],
                       capture_output=True, text=True, timeout=60)
        print("        git pull ok")
    except Exception as exc:
        print(f"        git pull skipped ({type(exc).__name__})")


def _keys() -> list[str]:
    keys = [FEED_SETTINGS.odds_api_key.strip()]
    keys += [k.strip() for k in FEED_SETTINGS.api_keys]
    return [k for k in keys if k]


def _get(url: str) -> tuple[object | None, str | None]:
    last_err = "no keys"
    for i, k in enumerate(_keys()):
        try:
            req = urllib.request.Request(
                url.replace("APIKEY", k),
                headers={"User-Agent": "sporty-edge/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code in (401, 429) and i + 1 < len(_keys()):
                continue
            return None, last_err
        except Exception as exc:
            last_err = type(exc).__name__
            continue
    return None, last_err


def _scores_map(needed_titles: set[str]) -> dict[str, dict]:
    smap: dict[str, dict] = {}
    if not needed_titles:
        return smap
    print(f"  [3/4] fetching final scores ({len(needed_titles)} league(s), "
          f"cap {MAX_LEAGUE_FETCHES})...")
    data, err = _get("https://api.the-odds-api.com/v4/sports?apiKey=APIKEY")
    if not data:
        print(f"        !! /sports failed ({err}) - skipping scores this cycle")
        return smap
    key_by_title = {s.get("title", ""): s["key"] for s in data}

    done = 0
    for title in sorted(needed_titles):
        if done >= MAX_LEAGUE_FETCHES:
            print(f"        cap reached - remaining leagues settle next cycle")
            break
        skey = key_by_title.get(title)
        if not skey:
            continue
        events, err = _get(
            f"https://api.the-odds-api.com/v4/sports/{skey}/scores"
            f"?daysFrom=3&apiKey=APIKEY")
        done += 1
        if not events:
            print(f"        {title[:30]:<30} no scores ({err})")
            continue
        n = 0
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
                n += 1
            except (TypeError, ValueError):
                continue
        print(f"        {title[:30]:<30} {n} finished games")

    print(f"         total: {len(smap)} final scores")
    return smap


def _leg_result(leg: dict, score: dict) -> str | None:
    m = str(leg.get("market", "")).upper()
    sel = str(leg.get("selection", ""))
    h, a = score["home"], score["away"]
    if m.startswith(("ML", "MONEYLINE", "1X2")):
        if sel == "Home":
            return "WIN" if h > a else "LOSS"
        if sel == "Away":
            return "WIN" if a > h else "LOSS"
        if sel == "Draw":
            return "WIN" if h == a else "LOSS"
    if m == "DC":
        if sel == "1X":
            return "WIN" if h >= a else "LOSS"
        if sel == "X2":
            return "WIN" if a >= h else "LOSS"
        if sel == "12":
            return "WIN" if h != a else "LOSS"
    if m.startswith("O/U"):
        try:
            line = float(m.split()[-1])
        except (ValueError, IndexError):
            return None
        total = h + a
        if sel == "Over":
            return "WIN" if total > line else "LOSS"
        if sel == "Under":
            return "WIN" if total < line else "LOSS"
    return None


def main() -> None:
    print("=" * 60)
    print("  settle_auto - final scores -> automatic settlement")
    print("=" * 60)
    _pull()

    from utils.logger import BetLogger
    lg = BetLogger()
    pending = lg.pending()
    print(f"  [2/4] pending bets: {len(pending)}")
    if not pending:
        print("  Nothing to settle.")
        return

    titles = {str(l.get("league") or "").strip()
              for r in pending for l in r.get("legs", [])} - {""}
    smap = _scores_map(titles)

    print("  [4/4] judging slips...")
    auto = 0
    ambiguous = 0
    for rec in pending:
        results = []
        for leg in rec.get("legs", []):
            sc = smap.get(str(leg.get("match_id") or "").upper())
            results.append(_leg_result(leg, sc) if sc else None)

        if all(r == "WIN" for r in results):
            lg.settle(rec["bet_id"], "WIN")
            auto += 1
            print(f"  {rec['bet_id']} -> WIN (auto)")
            continue
        if any(r == "LOSS" for r in results):
            lg.settle(rec["bet_id"], "LOSS")
            auto += 1
            print(f"  {rec['bet_id']} -> LOSS (auto)")
            continue
        if any(r is None for r in results):
            ambiguous += 1

    m = lg.metrics()
    print(f"\n  auto-settled {auto} | awaiting scores: {ambiguous} | "
          f"metrics: {m}")
    print("  Sync: git add data/bets.jsonl ; git commit -m 'Auto settle' "
          "; git pull --rebase -X theirs origin main ; git push")


if __name__ == "__main__":
    main()