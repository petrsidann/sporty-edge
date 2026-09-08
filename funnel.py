"""funnel.py - WHY is the session board empty? Prints the full funnel."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from odds.comparator import OddsComparator
from utils.session import current_or_next

CACHE = Path("data") / "feed_cache.json"
DUR = [("mlb", 2.9), ("kbo", 2.9), ("npb", 2.9), ("baseball", 2.9),
       ("nfl", 3.2), ("ncaaf", 3.3), ("ncaab", 2.7), ("nba", 2.4),
       ("nhl", 2.6), ("mma", 1.5), ("tennis", 2.2)]


def dur(league):
    s = (league or "").lower()
    for k, h in DUR:
        if k in s:
            return h
    return 2.05


def main() -> None:
    session, ws, we = current_or_next()
    now = datetime.now(timezone.utc)
    print(f"session {session.name} | window {ws.astimezone().strftime('%H:%M')}"
          f" -> {we.astimezone().strftime('%H:%M')} local")

    data = json.loads(CACHE.read_text(encoding="utf-8-sig"))
    kmap = {}
    for entry in (data.get("sports") or {}).values():
        for ev in entry.get("events") or []:
            eid = str(ev.get("id") or "")[:10].upper()
            if eid and ev.get("commence_time"):
                kmap[eid] = str(ev["commence_time"])

    from utils.logger import BetLogger
    lg = BetLogger()
    pend_m, pend_k = set(), set()
    for rec in lg.pending():
        for leg in rec.get("legs", []):
            pend_m.add(leg.get("match_id"))
            pend_k.add((leg.get("match_id"), leg.get("market"),
                        leg.get("selection")))

    comparator = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0)
    matches = defaultdict(lambda: {"best": 0.0, "odds": 0.0, "pending": False,
                                   "label": "", "late": 0, "league": ""})
    total_events = 0
    for entry in (data.get("sports") or {}).values():
        for ev in entry.get("events") or []:
            total_events += 1
            eid = str(ev.get("id") or "")[:10].upper()
            ts = kmap.get(eid)
            if not ts:
                continue
            ko = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if ko.tzinfo is None:
                ko = ko.replace(tzinfo=timezone.utc)
            if ko < now:
                continue
            league = str(ev.get("sport_title") or "")
            d = dur(league)
            if (ko + timedelta(hours=d)) > we:
                matches.setdefault(eid, {"late": 1})["late"] = 1
                continue
            m = matches.setdefault(eid, {"best": 0.0, "odds": 0.0,
                                         "pending": eid in pend_m,
                                         "label": f"{ev.get('home_team')} vs {ev.get('away_team')}",
                                         "late": 0, "league": league[:22]})
            for book in ev.get("bookmakers") or []:
                for mk in book.get("markets") or []:
                    if mk.get("key") not in ("h2h",):
                        continue
                    for o in mk.get("outcomes") or []:
                        px = float(o.get("price") or 0)
                        if px <= 1.01:
                            continue
                        p = 1.0 / px
                        if p > m["best"]:
                            m["best"], m["odds"] = p, px

    in_win = [m for m in matches.values() if m.get("label") and not m.get("late")]
    blocked = [m for m in in_win if m.get("pending")]
    open_m = [m for m in in_win if not m.get("pending")]
    lane_kill = [m for m in open_m if m["best"] < 0.50 and m["best"] > 0]
    good = sorted(open_m, key=lambda m: -m["best"])[:12]

    print(f"\n  cache events: {total_events}")
    print(f"  in-window matches      : {len(in_win)}")
    print(f"  blocked by pending bet : {len(blocked)}   <- one-bet-per-match rule")
    print(f"  open matches           : {len(open_m)}")
    print(f"  open but best prob<50% : {len(lane_kill)}   <- tight matches (need totals lane)")
    print(f"\n  Top open candidates (prob | odds | league | match):")
    for m in good:
        print(f"    {m['best']:.0%} | {m['odds']:.2f} | {m['league']:<22} | {m['label'][:34]}")
    blocked_top = sorted(blocked, key=lambda m: -m["best"])[:6]
    if blocked_top:
        print(f"\n  Blocked-but-live (already have a bet on these):")
        for m in blocked_top:
            print(f"    {m['best']:.0%} | {m['label'][:40]}")


if __name__ == "__main__":
    main()