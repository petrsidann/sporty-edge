"""
settle.py v3 - settlement that does the work itself.

    python settle.py

1. Pulls the latest ledger from GitHub (cloud-run bets live there).
2. Fetches REAL final scores from the feed's /scores endpoint.
3. Auto-settles every ML / 1X2 / O/U leg from the score; slips with all
   legs resolved settle automatically.  Anything ambiguous (spreads with
   unknown sign) is shown with the final score - one keystroke W/L/V.
4. Prints your record.  This IS the correct-stats engine you asked for.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from datetime import datetime, timezone

from config.settings import FEED_SETTINGS

_VALID = {"w": "WIN", "l": "LOSS", "v": "VOID"}


def _pull():
    try:
        r = subprocess.run(["git", "pull", "--rebase", "-X", "theirs",
                            "origin", "main"],
                           capture_output=True, text=True, timeout=60)
        out = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
        print(f"  git pull: {out[-1] if out else 'ok'}")
    except Exception as exc:
        print(f"  git pull skipped ({type(exc).__name__})")


def _keys() -> list[str]:
    keys = [FEED_SETTINGS.odds_api_key.strip()]
    keys += [k.strip() for k in FEED_SETTINGS.api_keys]
    return [k for k in keys if k]


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


def _scores_map(needed_titles: set[str]) -> dict[str, dict]:
    """{event_id[:10]: {'home': int, 'away': int}} for finished games."""
    smap: dict[str, dict] = {}
    if not needed_titles:
        return smap
    data, err = _get("https://api.the-odds-api.com/v4/sports?apiKey=APIKEY")
    if not data:
        print(f"  !! scores unavailable ({err}) - manual settlement only.")
        return smap
    key_by_title = {}
    for s in data:
        key_by_title.setdefault(s.get("title", ""), s["key"])
    for title in needed_titles:
        skey = key_by_title.get(title)
        if not skey:
            continue
        events, err = _get(
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
                smap[str(ev["id"])[:10].upper()] = {"home": int(hs), "away": int(as_)}
            except (TypeError, ValueError):
                continue
    return smap


def _leg_result(leg: dict, score: dict) -> str | None:
    """WIN/LOSS for one leg from a final score; None = cannot judge."""
    m = str(leg.get("market", "")).upper()
    sel = str(leg.get("selection", ""))
    h, a = score["home"], score["away"]
    if m.startswith("ML") or m.startswith("MONEYLINE") or m.startswith("1X2"):
        if sel == "Home":
            return "WIN" if h > a else "LOSS"
        if sel == "Away":
            return "WIN" if a > h else "LOSS"
        if sel == "Draw":
            return "WIN" if h == a else "LOSS"
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


def _age_hours(ts: str) -> float:
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return 0.0


def main() -> None:
    print("=" * 64)
    print("  settle.py v3 - auto-settlement from real final scores")
    print("=" * 64)
    _pull()

    from utils.logger import BetLogger
    lg = BetLogger()
    pending = lg.pending()
    if not pending:
        print("  Nothing pending.")
        return

    titles = {str(l.get("league") or "").strip()
              for r in pending for l in r.get("legs", [])} - {""}
    print(f"  Fetching final scores for {len(titles)} league(s) ...")
    smap = _scores_map(titles)
    print(f"  Got final scores for {len(smap)} finished game(s).")

    for rec in pending:
        legs = rec.get("legs", [])
        results = []
        for leg in legs:
            score = smap.get(str(leg.get("match_id") or "").upper())
            if score is None:
                results.append(None)
            else:
                results.append(_leg_result(leg, score))

        print("-" * 64)
        print(f"  {rec['bet_id']} [{rec.get('slip_type')}] "
              f"({rec.get('session')}, logged {_age_hours(rec.get('logged_at','')):.0f}h ago)")
        for i, (leg, res) in enumerate(zip(legs, results), start=1):
            score = smap.get(str(leg.get("match_id") or "").upper())
            fs = f"  final {score['home']}-{score['away']}" if score else ""
            mark = res or "??"
            print(f"    {i}. {leg.get('match_label','?')[:40]:<40} "
                  f"{leg.get('market')}->{leg.get('selection')} [{mark}]{fs}")

        if all(r == "WIN" for r in results):
            lg.settle(rec["bet_id"], "WIN")
            print("    AUTO-SETTLED: WIN (all legs verified from final scores)")
            continue
        if any(r == "LOSS" for r in results):
            lg.settle(rec["bet_id"], "LOSS")
            print("    AUTO-SETTLED: LOSS")
            continue
        while True:
            ans = input("    slip result? [W]in / [L]oss / [V]oid / [s]kip: ").strip().lower()
            if ans in ("s", ""):
                break
            out = _VALID.get(ans)
            if out:
                try:
                    lg.settle(rec["bet_id"], out)
                    print(f"    OK settled {out}.")
                except ValueError as exc:
                    print(f"    ! {exc}")
                break

    print("=" * 64)
    m = lg.metrics()
    print("  Metrics:", m)
    n = int(m.get("settled", 0))
    print(f"  {n}/25 settled toward the first tier promotion."
          if n < 25 else "  Tier gate reached - stakes scale next run.")
    print('  Sync: git add data/bets.jsonl ; git commit -m "Settle" '
          '; git pull --rebase -X theirs origin main ; git push')


if __name__ == "__main__":
    main()