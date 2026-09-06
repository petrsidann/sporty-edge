"""
smart_picks.py (ALWAYS-8, clean) - volume-first scanner.

For every match on the feed it takes the single highest-probability market
(goals totals usually rank highest - the professional answer to coin-flip
1X2s), ranks everything by win probability, and emits 8 picks across the
8 platforms:

    Pass 1: games that finish inside this session   -> [LIVE]
    Pass 2: fill to 8 from the next 24 hours        -> [NEXT-DAY]
    Pass 3: fill to 8 from 24-48 hours              -> [DAY+2]

Only excluded: matches you already have pending (never bet both sides of
the same game), duplicate picks, and prices >2.5x off consensus (broken
feed data).  Nothing else is filtered.  Win probability sets the stake
(HOT 1.0x / WARM 0.5x / COIN 0.25x / LONG 0.1x) so the ledger stays honest.
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
ABSURD_RATIO = 2.50
N_PICKS = 8
CACHE = Path("data") / "feed_cache.json"

DURATIONS = [
    ("mlb", 2.9), ("kbo", 2.9), ("npb", 2.9), ("baseball", 2.9),
    ("nfl", 3.2), ("ncaaf", 3.3), ("nba", 2.4), ("basketball", 2.4),
    ("nhl", 2.6), ("hockey", 2.6), ("mma", 1.5), ("tennis", 2.2),
]


def sport_duration(league: str) -> float:
    s = (league or "").lower()
    for key, hours in DURATIONS:
        if key in s:
            return hours
    return 2.05


def _teams(label: str) -> tuple[str, str]:
    left, _, right = label.partition(" vs ")
    return left.strip(), right.split(" · ")[0].strip()


def _anchor(market: str, selection: str, label: str) -> str:
    home, away = _teams(label)
    m = market.upper()
    if m.startswith(("ML", "MONEYLINE", "1X2")):
        if selection == "Home":
            return f"{home} to win"
        if selection == "Away":
            return f"{away} to win"
        return "Draw"
    if m.startswith("O/U"):
        line = market.replace("O/U ", "")
        return f"{selection} {line} goals"
    if m.startswith("DC"):
        return f"Double chance {selection}"
    return f"{market} -> {selection}"


def _pending(logger: BetLogger) -> tuple[set, set]:
    """(pending leg keys, pending match ids) - one bet per match, ever."""
    keys: set = set()
    matches: set = set()
    for rec in logger.pending():
        for leg in rec.get("legs", []):
            keys.add((leg.get("match_id"), leg.get("market"),
                      leg.get("selection")))
            matches.add(leg.get("match_id"))
    return keys, matches


def _kickoff_map() -> dict:
    out: dict = {}
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return out
    for entry in (data.get("sports") or {}).values():
        for ev in entry.get("events") or []:
            eid = str(ev.get("id") or "")[:10].upper()
            if eid and ev.get("commence_time"):
                out[eid] = str(ev["commence_time"])
    return out


def _kickoff_eat(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(EAT).strftime("%a %H:%M EAT")
    except (ValueError, TypeError):
        return "?"


def _kickoff_dt(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _tier(prob: float) -> tuple[str, float]:
    if prob >= 0.70:
        return "HOT", 1.00
    if prob >= 0.60:
        return "WARM", 0.50
    if prob >= 0.50:
        return "COIN", 0.25
    return "LONG", 0.10


def main() -> None:
    base_stake = 0.5
    if "--stake" in sys.argv:
        i = sys.argv.index("--stake")
        try:
            base_stake = float(sys.argv[i + 1])
        except (ValueError, IndexError):
            pass

    session = detect()
    tg = TelegramNotifier()
    logger = BetLogger()
    now = datetime.now(timezone.utc)
    next_start = next_start_eat(now)

    print("=" * 70)
    print(f"  SMART PICKS ALWAYS-8 | {session.emoji} {session.name}")
    print(f"  {clock_line()}")
    print("=" * 70)

    cands = OddsApiFeed(min_hours_ahead=0.0, max_hours_ahead=48.0,
                        markets="h2h,totals").collect()
    if not cands:
        msg = "[FAIL] feed returned zero events - run: python doctor.py"
        print(msg)
        if tg.is_configured:
            tg.send(msg)
        return

    comparator = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0)
    pending_keys, pending_matches = _pending(logger)
    kmap = _kickoff_map()

    # best (highest-probability) market per match
    best_per_match: dict[str, BetOpportunity] = {}
    for sel, quotes in cands:
        opp = comparator.evaluate(sel, quotes)
        prob = opp.selection.model_probability
        if not 0.02 <= prob <= 0.98:
            continue
        if opp.decimal_odds / (1.0 / prob) > ABSURD_RATIO:
            continue  # broken feed line, not a real price
        key = (opp.selection.match_id, opp.selection.market,
               opp.selection.selection)
        if key in pending_keys:
            continue
        if opp.selection.match_id in pending_matches:
            continue  # never bet both sides of a game already pending
        cur = best_per_match.get(opp.selection.match_id)
        if cur is None or prob > cur.selection.model_probability:
            best_per_match[opp.selection.match_id] = opp

    # zone split: LIVE (this session) -> NEXT-DAY -> DAY+2
    live: list[BetOpportunity] = []
    nxt: list[BetOpportunity] = []
    d2: list[BetOpportunity] = []
    for opp in best_per_match.values():
        ko = _kickoff_dt(kmap.get(opp.selection.match_id, ""))
        if ko is None:
            d2.append(opp)
            continue
        dur = sport_duration(opp.selection.league)
        if ko >= now and (ko + timedelta(hours=dur)) <= next_start:
            live.append(opp)
        elif ko < next_start + timedelta(hours=24):
            nxt.append(opp)
        else:
            d2.append(opp)

    live.sort(key=lambda o: o.selection.model_probability, reverse=True)
    nxt.sort(key=lambda o: o.selection.model_probability, reverse=True)
    d2.sort(key=lambda o: o.selection.model_probability, reverse=True)

    tagged: list[tuple[BetOpportunity, str]] = (
        [(o, "LIVE") for o in live]
        + [(o, "NEXT-DAY") for o in nxt]
        + [(o, "DAY+2") for o in d2]
    )
    picks = tagged[:N_PICKS]

    print(f"\n  Supply: {len(live)} in-session | {len(nxt)} next-day | "
          f"{len(d2)} day+2  ->  top {len(picks)} by win probability\n")

    if not picks:
        msg = "[FAIL] no events at all on the feed - run: python doctor.py"
        print(msg)
        if tg.is_configured:
            tg.send(msg)
        return

    bankroll = Bankroll()
    placed: list[tuple[str, BetOpportunity, str, str, float]] = []

    for i, (opp, zone) in enumerate(picks, start=1):
        platform = PLATFORMS[i - 1]
        prob = opp.selection.model_probability
        label, mult = _tier(prob)
        eff = round(base_stake * mult, 2)
        slip = Slip(slip_type="SINGLE",
                    legs=[SlipLeg.from_opportunity(opp)],
                    stake_units=eff)
        if not bankroll.can_place(eff):
            print(f"  !! exposure cap blocked {platform} - settle pending first.")
            continue
        bankroll.register_bet(eff)
        bet_id = logger.log_slip(slip, session=session.name)
        placed.append((platform, opp, bet_id, label, eff))

        leg = slip.legs[0]
        floor = round(leg.decimal_odds * 0.97, 2)
        ko = _kickoff_dt(kmap.get(leg.match_id, ""))
        done = ((ko + timedelta(hours=sport_duration(opp.selection.league)))
                .astimezone(EAT).strftime("%H:%M EAT") if ko else "?")
        msg = (f"{zone} {session.name} {i}/{len(picks)} -> "
               f"{platform.upper()} [{label}]\n"
               f"{leg.match_label.split(' · ')[0]}\n"
               f"KICKOFF {_kickoff_eat(kmap.get(leg.match_id, ''))} | "
               f"settles ~{done}\n"
               f"PICK: {_anchor(leg.market, leg.selection, leg.match_label)}\n"
               f"Win prob {prob:.0%} | take {leg.decimal_odds:.2f} | "
               f"place if app >= {floor}\n"
               f"Stake {eff:.2f}u | {bet_id}")
        print(msg + "\n")
        if tg.is_configured:
            tg.send(msg)

    if placed:
        exp = sum(o.ev_per_unit * e for _, o, _, _, e in placed)
        total = sum(e for _, _, _, _, e in placed)
        summary = (f"SMART PICKS {session.name}: {len(placed)}/8 delivered | "
                   f"stake {total:.2f}u | expected P/L {exp:+.2f}u\n"
                   f"Place what clears the floors. Click TEAM NAMES.\n"
                   f"Settle + next picks anytime: python run_now.py")
        print(summary)
        if tg.is_configured:
            tg.send(summary)


if __name__ == "__main__":
    main()