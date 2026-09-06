"""
single_shot.py (FINAL) - 8 singles per session, 8 platforms, session-bound.

Quality sets the stake: EDGE+ 1.0x | NEUTRAL 0.5x | FUN 0.25x | FILLER 0.1x.
Strict settle-inside-session rule with per-sport durations. Dedupe-safe.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from feeds.oddsapi import OddsApiFeed
from notify.telegram import TelegramNotifier
from odds.comparator import BetOpportunity, OddsComparator
from slips.generator import Slip, SlipLeg
from utils.bankroll import Bankroll
from utils.logger import BetLogger
from utils.session import EAT, clock_line, detect, next_start_eat

PLATFORMS: list[str] = [
    "SportyBet", "Betika", "1xBet", "BetPawa",
    "MozzartBet", "LuckyPari", "BetJam", "WekaWin",
]
SUSPECT_RATIO = 1.20
N_PICKS = 8
MIN_ODDS = 1.10
MAX_ODDS = 2.30
SANITY_PROB = 0.40
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


def _teams(label: str):
    left, _, right = label.partition(" vs ")
    away = right.split(" · ")[0].strip()
    return left.strip(), away


def _anchor(market, selection, label):
    home, away = _teams(label)
    m = market.upper()
    if m.startswith(("ML", "MONEYLINE", "1X2")):
        if selection == "Home": return f"{home} to win"
        if selection == "Away": return f"{away} to win"
        return "Draw"
    if m.startswith("DC"): return f"Double chance {selection}"
    if m.startswith("O/U"): return f"{market.replace('O/U ', '')} {selection} (match total)"
    return f"{market} -> {selection}"


def _is_suspect(o): return o.decimal_odds / (1.0 / o.selection.model_probability) > SUSPECT_RATIO


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


def _flag(argv, name, default):
    if name in argv:
        i = argv.index(name)
        try: return float(argv[i + 1])
        except (ValueError, IndexError): return default
    return default


def _tier(prob, ev):
    if prob >= 0.70 and ev >= 0.02: return "EDGE+", 1.00
    if prob >= 0.60 and ev >= -0.02: return "NEUTRAL", 0.50
    if prob >= 0.50: return "FUN", 0.25
    return "FILLER", 0.10


def main():
    base_stake = _flag(sys.argv, "--stake", 0.5)
    platforms = PLATFORMS
    session = detect()
    tg = TelegramNotifier()
    logger = BetLogger()
    if tg.is_configured:
        tg.send(f"⏰ {session.emoji} {session.name} singles scan...")

    now = datetime.now(timezone.utc)
    next_start = next_start_eat(now)
    cands = OddsApiFeed(min_hours_ahead=0.0,
                        max_hours_ahead=(next_start - now).total_seconds() / 3600.0).collect()

    comparator = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0)
    pending = _pending_keys(logger)
    kmap = _kickoff_map()

    opps = [comparator.evaluate(s, q) for s, q in cands]
    opps = [o for o in opps if not _is_suspect(o)]

    by_match = defaultdict(list)
    for o in opps:
        by_match[o.selection.match_id].append(o)

    pool = []
    for group in by_match.values():
        best = max(group, key=lambda o: o.selection.model_probability)
        if best.selection.market.upper().startswith("SPREAD"): continue
        if not (MIN_ODDS <= best.decimal_odds <= MAX_ODDS): continue
        if best.selection.model_probability < SANITY_PROB: continue
        if len(best.quotes_by_book) < 2: continue
        k = (best.selection.match_id, best.selection.market, best.selection.selection)
        if k in pending: continue
        ko = _kickoff_dt(kmap.get(best.selection.match_id, ""))
        if ko is None: continue
        if (ko + timedelta(hours=sport_duration(best.selection.league))) > next_start: continue
        pool.append(best)

    pool.sort(key=lambda o: o.selection.model_probability, reverse=True)
    picks = pool[:N_PICKS]

    if not picks:
        if tg.is_configured:
            tg.send(f"{session.emoji} {session.name}: no in-session games right now — autoscan continues on the next cycle.")
        return

    bankroll = Bankroll()
    placed = []
    for i, o in enumerate(picks, start=1):
        platform = platforms[i - 1]
        label, mult = _tier(o.selection.model_probability, o.ev_per_unit)
        eff = round(base_stake * mult, 2)
        slip = Slip(slip_type="SINGLE", legs=[SlipLeg.from_opportunity(o)], stake_units=eff)
        if not bankroll.can_place(eff): continue
        bankroll.register_bet(eff)
        bet_id = logger.log_slip(slip, session=session.name)
        placed.append((platform, o, bet_id, label, eff))
        leg = slip.legs[0]
        floor = round(leg.decimal_odds * 0.97, 2)
        ko = _kickoff_dt(kmap.get(leg.match_id, ""))
        done = ((ko + timedelta(hours=sport_duration(o.selection.league))).astimezone(EAT).strftime("%H:%M EAT") if ko else "?")
        msg = (f"SINGLE {session.name} {i}/{len(picks)} -> {platform.upper()} [{label}]\n"
               f"{leg.match_label.split(' · ')[0]}\n"
               f"KICKOFF {_kickoff_eat(kmap.get(leg.match_id, ''))} | settles ~{done}\n"
               f"PICK: {_anchor(leg.market, leg.selection, leg.match_label)}\n"
               f"Win prob {leg.model_prob:.0%} | take {leg.decimal_odds:.2f} | place if app >= {floor}\n"
               f"Stake {eff:.2f}u | {bet_id}")
        print(msg)
        if tg.is_configured: tg.send(msg)

    if placed:
        exp = sum(o.ev_per_unit * e for _, o, _, _, e in placed)
        avg_p = sum(o.selection.model_probability for _, o, _, _, _ in placed) / len(placed)
        from utils.session import clock_line as _cl
        msg2 = (f"SINGLES {session.name}: +{len(placed)} new (total today see ledger) | "
                f"avg win prob {avg_p:.0%} | expected {exp:+.2f}u")
        if tg.is_configured: tg.send(msg2)


if __name__ == "__main__":
    main()