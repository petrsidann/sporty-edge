"""
totals.py - TOTALS specialist: Over/Under every line, every match, window-bound.

    python totals.py               # 8 totals picks, quality-graded stakes
    python totals.py --min-prob 0.70
    python totals.py --stake 0.5

Two lanes in one pool:
  LINE-SHOP EDGE : a book pays above cross-book consensus on a line (real,
                   measured disagreement - the classic totals edge)
  SAFE TOTALS    : consensus probability >= min-prob (e.g. Over 0.5, U 4.5)
                   - high hit-rate, small payout, honest about the tail

Same contract as singles: every pick FINISHES inside the session window
(per-sport durations), quality sets the stake (EDGE+/NEUTRAL/FUN/FILLER),
8 picks target, shortfall reported honestly, Telegram delivery.
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
from odds.comparator import BetOpportunity, OddsComparator, Selection
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
SANITY_PROB = 0.40
GAME_HOURS = 2.5
_CACHE = Path("data") / "feed_cache.json"

DURATIONS: list[tuple[str, float]] = [
    ("mlb", 2.9), ("kbo", 2.9), ("npb", 2.9), ("baseball", 2.9),
    ("nfl", 3.2), ("ncaaf", 3.3),
    ("nba", 2.4), ("basketball", 2.4),
    ("nhl", 2.6), ("hockey", 2.6),
    ("mma", 1.5), ("tennis", 2.2),
]


def sport_duration(league: str) -> float:
    s = (league or "").lower()
    for key, hours in DURATIONS:
        if key in s:
            return hours
    return 2.05


def _devig_pair(over: float, under: float) -> float | None:
    """Power-method de-margin for a 2-way market -> P(Over)."""
    qo, qu = 1.0 / over, 1.0 / under
    total = qo + qu
    if total <= 1.0:
        return qo / total if total > 0 else None
    lo, hi = 1.0, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if qo**mid + qu**mid > 1.0:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2.0
    po = qo**k
    return po / (po + qu**k)


def _teams(label: str) -> tuple[str, str]:
    left, _, right = label.partition(" vs ")
    away = right.split(" · ")[0].strip()
    return left.strip(), away


def _pending_keys(logger: BetLogger):
    keys = set()
    for rec in logger.pending():
        for leg in rec.get("legs", []):
            keys.add((leg.get("match_id"), leg.get("market"), leg.get("selection")))
    return keys


def _kickoff_map() -> dict:
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


def _flag(argv, name, default):
    if name in argv:
        i = argv.index(name)
        try:
            return float(argv[i + 1])
        except (ValueError, IndexError):
            return default
    return default


def _tier(prob: float, ev: float) -> tuple[str, float]:
    if prob >= 0.70 and ev >= 0.02:
        return "EDGE+", 1.00
    if prob >= 0.60 and ev >= -0.02:
        return "NEUTRAL", 0.50
    if prob >= 0.50:
        return "FUN", 0.25
    return "FILLER", 0.10


def main() -> None:
    min_prob = _flag(sys.argv, "--min-prob", 0.70)
    base_stake = _flag(sys.argv, "--stake", 0.5)
    session = detect()
    tg = TelegramNotifier()
    logger = BetLogger()

    now = datetime.now(timezone.utc)
    next_start = next_start_eat(now)
    print("=" * 70)
    print(f"  TOTALS | {session.emoji} {session.name} | "
          f"{N_PICKS} O/U picks | all settle in-session")
    print(f"  {clock_line()}")
    print("=" * 70)

    feed = OddsApiFeed(
        min_hours_ahead=0.0,
        max_hours_ahead=(next_start - now).total_seconds() / 3600.0,
        markets="h2h,totals",
    )
    if not feed.is_configured:
        print("\n  [FAIL] NO API KEYS LOADED. Run:  python doctor.py")
        return
    cands = feed.collect()

    pending = _pending_keys(logger)
    kmap = _kickoff_map()
    comparator = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0)

    # Rebuild the totals board from raw feed cache (feed.collect() already
    # parsed h2h; totals need their own pass, so re-derive from cache):
    supply: dict[str, int] = defaultdict(int)
    pool: list[tuple[BetOpportunity, str]] = []   # (opp, lane)

    # -- derive totals candidates directly from the cached events --
    try:
        cache = json.loads(_CACHE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        cache = {}

    for entry in (cache.get("sports") or {}).values():
        for ev in entry.get("events") or []:
            ts = str(ev.get("commence_time") or "")
            ko = _kickoff_dt(ts)
            if ko is None:
                continue
            league = str(ev.get("sport_title") or "")
            if (ko + timedelta(hours=sport_duration(league))) > next_start:
                continue  # must settle inside this session
            label = (f"{ev.get('home_team', '?')} vs {ev.get('away_team', '?')}"
                     f" · {_kickoff_eat(ts)}")
            match_id = str(ev.get("id") or "")[:10].upper()

            # collect per-book totals at the most common line
            by_line: dict[float, dict[str, dict[str, float]]] = defaultdict(dict)
            for book in ev.get("bookmakers") or []:
                bname = book.get("title") or book.get("key") or "?"
                for market in book.get("markets") or []:
                    if market.get("key") != "totals":
                        continue
                    pts = market.get("points")
                    if pts is None:
                        continue
                    line = float(pts)
                    for out in market.get("outcomes") or []:
                        side = out.get("name")
                        price = float(out.get("price") or 0.0)
                        if side in ("Over", "Under") and price > 1.01:
                            by_line[line].setdefault(bname, {})[side] = price

            # evaluate the 1-2 most-covered lines per event
            ranked_lines = sorted(by_line.items(),
                                  key=lambda kv: len(kv[1]), reverse=True)[:2]
            for line, books in ranked_lines:
                if len(books) < 2:
                    continue
                over_ps: list[float] = []
                for sides in books.values():
                    if "Over" not in sides or "Under" not in sides:
                        continue
                    p = _devig_pair(sides["Over"], sides["Under"])
                    if p is not None:
                        over_ps.append(p)
                if not over_ps:
                    continue
                consensus_over = statistics.median(over_ps)
                for pick, prob in (("Over", consensus_over),
                                   ("Under", 1.0 - consensus_over)):
                    if not 0.05 <= prob <= 0.95:
                        continue
                    # best book price for this side
                    best_name, best_price = None, 0.0
                    for bname, sides in books.items():
                        px = sides.get(pick, 0.0)
                        if px > best_price:
                            best_name, best_price = bname, px
                    if best_price < 1.10:
                        continue
                    ev_ = prob * best_price - 1.0
                    lane = ("LINE-SHOP" if ev_ >= 0.03 else
                            "SAFE" if prob >= min_prob else "TOT")
                    sel = Selection(
                        match_id=match_id, match_label=label, league=league,
                        market=f"O/U {line:g}", selection=pick,
                        model_probability=prob,
                    )
                    opp = comparator.evaluate(sel, [
                        type(feed) and __import__("odds.comparator",
                                                  fromlist=["OddsQuote"]
                                                  ).OddsQuote(
                            book=best_name, decimal_odds=best_price)
                    ])
                    if opp.decimal_odds / (1.0 / prob) > SUSPECT_RATIO:
                        continue
                    k = (match_id, opp.selection.market, pick)
                    if k in pending:
                        continue
                    pool.append((opp, lane))
                    supply[league[:20]] += 1

    pool.sort(key=lambda t: t[0].selection.model_probability, reverse=True)
    picks = pool[:N_PICKS]

    print(f"\n  Totals supply: "
          + (", ".join(f"{k}:{v}" for k, v in
                       sorted(supply.items(), key=lambda x: -x[1])) or "none"))

    if not picks:
        print("\n  No totals priced inside this session's window right now.")
        if tg.is_configured:
            tg.send(f"TOTALS {session.name}: no in-window totals found.")
        return

    bankroll = Bankroll()
    placed = []
    for i, (opp, lane) in enumerate(picks, start=1):
        platform = PLATFORMS[i - 1]
        prob = opp.selection.model_probability
        label, mult = _tier(prob, opp.ev_per_unit)
        if lane == "SAFE" and label == "FILLER":
            label, mult = "FUN", 0.25   # safe lane never drops to token stake
        eff = round(base_stake * mult, 2)
        slip = Slip(slip_type="SINGLE",
                    legs=[SlipLeg.from_opportunity(opp)],
                    stake_units=eff)
        if not bankroll.can_place(eff):
            print(f"  !! exposure cap blocked {platform}.")
            continue
        bankroll.register_bet(eff)
        bet_id = logger.log_slip(slip, session=session.name)
        placed.append((platform, opp, bet_id, label, eff))
        leg = slip.legs[0]
        floor = round(leg.decimal_odds * 0.97, 2)
        ko = _kickoff_dt(kmap.get(leg.match_id, ""))
        done = ((ko + timedelta(hours=sport_duration(opp.selection.league)))
                .astimezone(EAT).strftime("%H:%M EAT") if ko else "?")
        line_txt = leg.match_label.split(" · ")[0]
        print(f"  {i}. [{label:<7} {lane:<9}] {platform.upper()}")
        print(f"     MATCH   : {line_txt}")
        print(f"     KICKOFF : {_kickoff_eat(kmap.get(leg.match_id, ''))} "
              f"| settles ~{done}")
        print(f"     PICK    : {leg.market} -> {leg.selection}")
        print(f"     PRICE   : take {leg.decimal_odds:.2f} "
              f"({best_name if False else opp.book}) | place if app >= {floor}")
        print(f"     MODEL   : {prob:.0%} | EV {opp.ev_per_unit * 100:+.1f}% "
              f"| stake {eff:.2f}u | {bet_id}\n")
        if tg.is_configured:
            tg.send(
                f"TOTAL {session.name} {i}/{len(picks)} -> {platform.upper()} "
                f"[{label} {lane}]\n"
                f"{line_txt}\n"
                f"KICKOFF {_kickoff_eat(kmap.get(leg.match_id, ''))} | "
                f"settles ~{done}\n"
                f"PICK: {leg.market} -> {leg.selection}\n"
                f"Win prob {prob:.0%} | take {leg.decimal_odds:.2f} | "
                f"place if app >= {floor}\n"
                f"Stake {eff:.2f}u | {bet_id}"
            )

    if placed:
        exp = sum(o.ev_per_unit * e for _, o, _, _, e in placed)
        avg_p = sum(o.selection.model_probability
                    for _, o, _, _, _ in placed) / len(placed)
        msg = (f"TOTALS {session.name}: {len(placed)} picks, "
               f"avg win prob {avg_p:.0%}, expected {exp:+.2f}u, "
               f"stake {sum(e for _, _, _, _, e in placed):.2f}u.")
        print("\n" + msg)
        if tg.is_configured:
            tg.send(msg)
    print("\n  Settle:  python settle.py  (totals auto-settle from final scores)")


if __name__ == "__main__":
    main()