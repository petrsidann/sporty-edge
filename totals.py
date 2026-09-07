"""
totals.py (FINAL) - O/U specialist. Session-bound, cache-robust, dedupe-safe.
Forces a totals refresh by fetching with markets=h2h,totals each cycle.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from feeds.oddsapi import OddsApiFeed
from notify.telegram import TelegramNotifier
from odds.comparator import BetOpportunity, OddsComparator, Selection, OddsQuote
from slips.generator import Slip, SlipLeg
from config.settings import BET_TARGET_PLATFORMS
from utils.bankroll import Bankroll
from utils.logger import BetLogger
from utils.session import EAT, clock_line, detect, next_start_eat
from utils.term import force_utf8_stdio

PLATFORMS: list[str] = BET_TARGET_PLATFORMS
SUSPECT_RATIO = 1.20
N_PICKS = 8
_CACHE = Path("data") / "feed_cache.json"

DURATIONS = [
    ("mlb", 2.9), ("kbo", 2.9), ("npb", 2.9), ("baseball", 2.9),
    ("nfl", 3.2), ("ncaaf", 3.3), ("nba", 2.4), ("basketball", 2.4),
    ("nhl", 2.6), ("hockey", 2.6), ("mma", 1.5), ("tennis", 2.2),
]


def sport_duration(league: str) -> float:
    s = (league or "").lower()
    for k, h in DURATIONS:
        if k in s:
            return h
    return 2.05


def _devig_pair(over, under):
    qo, qu = 1.0 / over, 1.0 / under
    total = qo + qu
    if total <= 1.0: return qo / total if total > 0 else None
    lo, hi = 1.0, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if qo**mid + qu**mid > 1.0: lo = mid
        else: hi = mid
    k = (lo + hi) / 2.0
    po = qo**k
    return po / (po + qu**k)


def _pending_keys(logger):
    keys = set()
    for rec in logger.pending():
        for leg in rec.get("legs", []):
            keys.add((leg.get("match_id"), leg.get("market"), leg.get("selection")))
    return keys


def _kickoff_map():
    out = {}
    try:
        data = json.loads(_CACHE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return out
    for entry in (data.get("sports") or {}).values():
        for ev in entry.get("events") or []:
            eid = str(ev.get("id") or "")[:10].upper()
            if eid and ev.get("commence_time"):
                out[eid] = str(ev["commence_time"])
    return out


def _kickoff_eat(ts):
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(EAT).strftime("%a %H:%M EAT")
    except (ValueError, TypeError):
        return "?"


def _kickoff_dt(ts):
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _tier(prob, ev):
    if prob >= 0.70 and ev >= 0.02: return "EDGE+", 1.00
    if prob >= 0.60 and ev >= -0.02: return "NEUTRAL", 0.50
    if prob >= 0.50: return "FUN", 0.25
    return "FILLER", 0.10


def main():
    force_utf8_stdio()
    session = detect()
    tg = TelegramNotifier()
    logger = BetLogger()
    if tg.is_configured:
        tg.send(f"⏰ {session.emoji} {session.name} totals scan...")

    now = datetime.now(timezone.utc)
    next_start = next_start_eat(now)
    # FORCE totals data: fresh fetch with both markets for this pass
    OddsApiFeed(min_hours_ahead=0.0,
                max_hours_ahead=(next_start - now).total_seconds() / 3600.0,
                markets="h2h,totals").collect()

    pending = _pending_keys(logger)
    kmap = _kickoff_map()
    comparator = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0)

    try:
        cache = json.loads(_CACHE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        cache = {}

    pool = []
    supply = defaultdict(int)
    for entry in (cache.get("sports") or {}).values():
        for ev in entry.get("events") or []:
            ts = str(ev.get("commence_time") or "")
            ko = _kickoff_dt(ts)
            if ko is None: continue
            league = str(ev.get("sport_title") or "")
            if (ko + timedelta(hours=sport_duration(league))) > next_start: continue
            label = f"{ev.get('home_team','?')} vs {ev.get('away_team','?')} · {_kickoff_eat(ts)}"
            match_id = str(ev.get("id") or "")[:10].upper()

            by_line = defaultdict(dict)
            for book in ev.get("bookmakers") or []:
                bname = book.get("title") or book.get("key") or "?"
                for market in book.get("markets") or []:
                    if market.get("key") != "totals": continue
                    pts = market.get("points")
                    if pts is None: continue
                    line = float(pts)
                    for out in market.get("outcomes") or []:
                        side, price = out.get("name"), float(out.get("price") or 0.0)
                        if side in ("Over", "Under") and price > 1.01:
                            by_line[line].setdefault(bname, {})[side] = price

            for line, books in sorted(by_line.items(), key=lambda kv: len(kv[1]), reverse=True)[:2]:
                if len(books) < 2: continue
                over_ps = []
                for sides in books.values():
                    if "Over" in sides and "Under" in sides:
                        p = _devig_pair(sides["Over"], sides["Under"])
                        if p is not None: over_ps.append(p)
                if not over_ps: continue
                cons_over = statistics.median(over_ps)
                for pick, prob in (("Over", cons_over), ("Under", 1.0 - cons_over)):
                    if not 0.30 <= prob <= 0.80: continue
                    best_name, best_px = None, 0.0
                    for bname, sides in books.items():
                        px = sides.get(pick, 0.0)
                        if px > best_px: best_name, best_px = bname, px
                    if best_px < 1.10: continue
                    opp = comparator.evaluate(
                        Selection(match_id=match_id, match_label=label, league=league,
                                  market=f"O/U {line:g}", selection=pick, model_probability=prob),
                        [OddsQuote(book=best_name, decimal_odds=best_px)])
                    if opp.decimal_odds / (1.0 / prob) > SUSPECT_RATIO: continue
                    k = (match_id, opp.selection.market, pick)
                    if k in pending: continue
                    pool.append(opp)
                    supply[league[:20]] += 1

    pool.sort(key=lambda o: o.selection.model_probability, reverse=True)
    picks = pool[:N_PICKS]

    if not picks:
        if tg.is_configured:
            tg.send(f"TOTALS {session.name}: no totals priced in-window this cycle — next 30-min cycle rescans.")
        return

    bankroll = Bankroll()
    for i, opp in enumerate(picks, start=1):
        platform = PLATFORMS[i % len(PLATFORMS)]
        prob = opp.selection.model_probability
        label, mult = _tier(prob, opp.ev_per_unit)
        eff = round(0.5 * mult, 2)
        slip = Slip(slip_type="SINGLE", legs=[SlipLeg.from_opportunity(opp)], stake_units=eff)
        if not bankroll.can_place(eff): continue
        bankroll.register_bet(eff)
        bet_id = logger.log_slip(slip, session=session.name)
        leg = slip.legs[0]
        floor = round(leg.decimal_odds * 0.97, 2)
        ko = _kickoff_dt(kmap.get(leg.match_id, ""))
        done = ((ko + timedelta(hours=sport_duration(opp.selection.league))).astimezone(EAT).strftime("%H:%M EAT") if ko else "?")
        msg = (f"TOTAL {session.name} {i}/{len(picks)} -> {platform.upper()} [{label}]\n"
               f"{leg.match_label.split(' · ')[0]}\n"
               f"KICKOFF {_kickoff_eat(kmap.get(leg.match_id, ''))} | settles ~{done}\n"
               f"PICK: {leg.market} -> {leg.selection}\n"
               f"Win prob {prob:.0%} | take {leg.decimal_odds:.2f} | place if app >= {floor}\n"
               f"Stake {eff:.2f}u | {bet_id}")
        print(msg)
        if tg.is_configured: tg.send(msg)


if __name__ == "__main__":
    main()