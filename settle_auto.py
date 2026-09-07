"""
settle_auto.py - non-interactive settlement (for cron + humans in a rush).

    python settle_auto.py

Pulls the ledger from GitHub, fetches real final scores, auto-settles every
slip it can judge (moneyline / 1X2 / totals / double chance).  Ambiguous
slips stay PENDING for python settle.py (interactive).

CLV: before judging, legs kicking off within the closing window get their
final pre-kickoff price recorded (utils/clv.snapshot_closing -- the feed
stops listing prices after kickoff, so this snapshot IS the closing line).
After judging, closing_odds and clv are attached onto every ledger leg
(clv = taken_odds / closing_odds - 1; missing data stays None, never 0).
"""

from __future__ import annotations

import json
import subprocess
import urllib.request

from config.settings import FEED_SETTINGS
from utils.term import force_utf8_stdio


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
    force_utf8_stdio()
    print("=" * 60)
    print("  settle_auto - final scores -> automatic settlement")
    print("=" * 60)
    _pull()

    from utils.logger import BetLogger
    lg = BetLogger()
    pending = lg.pending()
    if not pending:
        print("  Nothing pending.")
        _clv_attach(lg)
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

    # ---- CLV: capture the closing line for legs near kickoff -------------- #
    # Runs BEFORE settlement: only pre-kickoff events still carry prices, and
    # only sports with an event inside the closing window are refreshed.
    try:
        from utils.clv import snapshot_closing

        snapshot_closing(lg.pending())
    except Exception as exc:  # CLV is instrumentation; never kill settlement
        print(f"  CLV: closing snapshot skipped ({exc!r})")

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
            elif m.startswith("DC"):
                # Double chance: 1X = home or draw, X2 = draw or away,
                # 12 = not a draw.  Required for the [HIT] lane to settle.
                if sel == "1X":
                    results.append("WIN" if h >= a else "LOSS")
                elif sel == "X2":
                    results.append("WIN" if a >= h else "LOSS")
                elif sel == "12":
                    results.append("WIN" if h != a else "LOSS")
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

    _clv_attach(lg)  # stamp closing_odds + clv onto every ledger leg
    m = lg.metrics()
    print(f"\n  auto-settled {auto} | metrics: {m}")
    if m.get("clv_legs"):
        print(f"  CLV: beat close {m['beat_close_rate'] * 100:.0f}% | "
              f"avg CLV {m['avg_clv'] * 100:+.2f}%")
    print("  Ambiguous slips (spreads etc.) stay pending for python settle.py")
    print('  Sync: git add data/bets.jsonl ; git commit -m "Auto settle" '
          '; git pull --rebase -X theirs origin main ; git push')


def _clv_attach(lg) -> None:
    """Attach closing_odds / clv onto ledger legs (best-effort)."""
    try:
        from utils.clv import attach_closing_odds

        n = attach_closing_odds(lg)
        if n:
            print(f"  CLV: attached closing odds to {n} ledger leg(s).")
    except Exception as exc:
        print(f"  CLV: enrichment skipped ({exc!r})")


if __name__ == "__main__":
    main()