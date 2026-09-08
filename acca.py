"""
acca.py - LOW-ODDS ACCUMULATOR engine (owner strategy).

Strategy: legs at odds 1.05-1.40 (any market: ML, O/U), combined into
3-5 leg accas targeting combined odds ~2.0-4.0 for bigger payouts.

Honesty contract (non-negotiable):
  * Every slip prints the TRUE combined win probability (legs multiply).
  * Slip is logged as slip_type "ACCA-LOW" so the ledger measures this
    strategy separately from everything else.
  * Expected math is shown on the slip: combined odds vs true probability.
    5 legs @ 1.20 lands ~33% of the time. The slip says so. Always.

Build rule: rank all legs in-window by win probability, take the highest
across different matches, pack into accas of --legs 3..5 (default 5),
never two legs from the same match (correlation kills the math).

    python acca.py --legs 5 --stake 0.5
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from utils.term import force_utf8_stdio
    force_utf8_stdio()
except Exception:
    pass

from feeds.oddsapi import OddsApiFeed, get_last_credits
from notify.telegram import TelegramNotifier
from odds.comparator import BetOpportunity, OddsComparator
from slips.generator import Slip, SlipLeg
from utils.bankroll import Bankroll
from utils.logger import BetLogger
from utils.session import EAT, current_or_next, clock_line

PLATFORMS: list[str] = ["BetPawa", "Betika", "LuckyPari", "WekaWin", "BetJam"]
CACHE = Path("data") / "feed_cache.json"
LEG_MIN_ODDS = 1.05
LEG_MAX_ODDS = 1.45          # the "easy win" zone
LEG_MIN_PROB = 0.55
ABSURD_RATIO = 2.5

DURATIONS = [
    ("mlb", 2.9), ("kbo", 2.9), ("npb", 2.9), ("baseball", 2.9),
    ("nfl", 3.2), ("ncaaf", 3.3), ("ncaab", 2.7), ("nba", 2.4),
    ("nhl", 2.6), ("mma", 1.5), ("tennis", 2.2),
]


def sport_duration(league: str) -> float:
    s = (league or "").lower()
    for k, h in DURATIONS:
        if k in s:
            return h
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
        return f"{selection} {market.replace('O/U ', '')} goals"
    if m == "DC":
        return f"Double chance {selection}"
    return f"{market} -> {selection}"


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


def _flag(argv, name, default):
    if name in argv:
        i = argv.index(name)
        try:
            return float(argv[i + 1]) if default.__class__ is float else int(argv[i + 1])
        except (ValueError, IndexError):
            return default
    return default


def main() -> None:
    n_legs = int(_flag(sys.argv, "--legs", 5))
    stake = _flag(sys.argv, "--stake", 0.5)
    n_slips = int(_flag(sys.argv, "--slips", 2))
    n_legs = max(3, min(n_legs, 8))

    session, win_start, win_end = current_or_next()
    tg = TelegramNotifier()
    logger = BetLogger()
    now = datetime.now(timezone.utc)
    hours = max(0.5, (win_end - now).total_seconds() / 3600.0)

    print("=" * 70)
    print(f"  ACCA-LOW | {session.emoji} {session.name} | {n_legs} legs x "
          f"{n_slips} slips @ {stake}u | odds {LEG_MIN_ODDS}-{LEG_MAX_ODDS}")
    print(f"  {clock_line()}")
    print("=" * 70)

    cands = OddsApiFeed(min_hours_ahead=0.0, max_hours_ahead=hours,
                        markets="h2h,totals").collect()
    if not cands:
        msg = "[FAIL] feed returned nothing - run: python doctor.py"
        print(msg)
        if tg.is_configured:
            tg.send(msg)
        return

    comparator = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0)
    kmap = _kickoff_map()
    pend_keys: set = set()
    pend_matches: set = set()
    for rec in logger.pending():
        for leg in rec.get("legs", []):
            pend_keys.add((leg.get("match_id"), leg.get("market"),
                           leg.get("selection")))
            pend_matches.add(leg.get("match_id"))

    # best low-odds market per match
    best: dict[str, BetOpportunity] = {}
    for sel, quotes in cands:
        opp = comparator.evaluate(sel, quotes)
        prob = opp.selection.model_probability
        if not LEG_MIN_PROB <= prob <= 0.97:
            continue
        if not LEG_MIN_ODDS <= opp.decimal_odds <= LEG_MAX_ODDS:
            continue
        if opp.decimal_odds / (1.0 / prob) > ABSURD_RATIO:
            continue
        ko = _kickoff_dt(kmap.get(opp.selection.match_id, ""))
        if ko is None or ko < now:
            continue
        if (ko + timedelta(hours=sport_duration(opp.selection.league))) > win_end:
            continue
        k = (opp.selection.match_id, opp.selection.market, opp.selection.selection)
        if k in pend_keys or opp.selection.match_id in pend_matches:
            continue
        cur = best.get(opp.selection.match_id)
        if cur is None or prob > cur.selection.model_probability:
            best[opp.selection.match_id] = opp

    pool = sorted(best.values(),
                  key=lambda o: o.selection.model_probability, reverse=True)

    print(f"\n  Leg pool: {len(pool)} qualifying legs "
          f"(need {n_legs * n_slips} for {n_slips} slips)")

    if len(pool) < n_legs:
        msg = (f"ACCA-LOW: only {len(pool)} legs >= {LEG_MIN_PROB:.0%} "
               f"this window - need {n_legs}. Next cycle rescans.")
        print(msg)
        if tg.is_configured:
            tg.send(msg)
        return

    bankroll = Bankroll()
    emitted = 0
    used_matches: set[str] = set()

    for slip_i in range(1, n_slips + 1):
        legs, used = [], set()
        odds_prod, prob_prod = 1.0, 1.0
        for o in pool:
            if len(legs) >= n_legs:
                break
            if o.selection.match_id in used or o.selection.match_id in used_matches:
                continue
            legs.append(o)
            used.add(o.selection.match_id)
            odds_prod *= o.decimal_odds
            prob_prod *= o.selection.model_probability
        if len(legs) < n_legs:
            break
        for o in legs:
            used_matches.add(o.selection.match_id)

        ev = prob_prod * odds_prod - 1.0
        slip = Slip(slip_type="ACCA-LOW",
                    legs=[SlipLeg.from_opportunity(o) for o in legs],
                    stake_units=stake)
        if not bankroll.can_place(stake):
            print("  !! exposure cap reached.")
            break
        bankroll.register_bet(stake)
        bet_id = logger.log_slip(slip, session=session.name)
        emitted += 1

        platform = PLATFORMS[(slip_i - 1) % len(PLATFORMS)]
        out = [f"ACCA-LOW {slip_i}/{n_slips} -> {platform.upper()} "
               f"({n_legs} legs)"]
        for j, o in enumerate(legs, start=1):
            hn, an = _teams(o.selection.match_label)
            ko = _kickoff_dt(kmap.get(o.selection.match_id, ""))
            out.append(
                f"  {j}. {o.selection.match_label.split(' · ')[0]} | "
                f"{_anchor(o.selection.market, o.selection.selection, o.selection.match_label)} "
                f"@ {o.decimal_odds:.2f} | "
                f"{_kickoff_eat(kmap.get(o.selection.match_id, ''))}")
        out.append(
            f"  Combined odds : {odds_prod:.2f}")
        out.append(
            f"  TRUE win prob : {prob_prod:.1%} "
            f"(~{prob_prod * 10:.0f} of 10) | break-even "
            f"{1.0 / odds_prod:.1%}")
        out.append(
            f"  EV            : {ev * 100:+.1f}% "
            f"{'(negative - this is the low-odds margin, priced in)' if ev < 0 else ''}")
        out.append(f"  Stake {stake:.2f}u | {bet_id} | credits ~{get_last_credits() or '?'}")
        msg = "\n".join(out)
        print(msg + "\n")
        if tg.is_configured:
            tg.send(msg)

    if tg.is_configured:
        tg.send(
            f"ACCA-LOW: {emitted} slip(s) built. Remember: legs multiply - "
            f"a 5-leg slip at avg 1.20 lands ~1 of 3. The ledger measures "
            f"this strategy separately (slip_type ACCA-LOW). Settle: python settle.py")


if __name__ == "__main__":
    main()