"""
smart_picks.py (ROLLING WINDOW) - run at ANY time, get 8 bets that all
settle within the NEXT 4 HOURS. That is the whole rule.

    python run_now.py

    * Window = [now, now+4h]. Every pick: kickoff inside it AND
      kickoff + sport duration <= window end. Nothing from tomorrow, ever.
    * Lanes: clear favorite -> pick the winner. Tight/draw-risky match ->
      pivot to the goals market (Over/Under) - scoring ignores the coin flip.
    * 8 picks, 8 platforms, ranked by win probability, stakes graded
      (HOT 1.0x / WARM 0.5x / STEADY 0.35x / SPICY 0.15x).
    * Dedupe: one bet per match, never duplicates, never both sides.
      Run again any time - only NEW picks are added.
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
from utils.session import EAT, clock_line, detect

PLATFORMS: list[str] = [
    "SportyBet", "Betika", "1xBet", "BetPawa",
    "MozzartBet", "LuckyPari", "BetJam", "WekaWin",
]
N_PICKS = 8
WINDOW_HOURS = 4.0
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
        return f"{selection} {market.replace('O/U ', '')} goals"
    if m.startswith("DC"):
        return f"Double chance {selection}"
    return f"{market} -> {selection}"


def _pending(logger: BetLogger) -> tuple[set, set]:
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
    if prob >= 0.75:
        return "HOT", 1.00
    if prob >= 0.65:
        return "WARM", 0.50
    if prob >= 0.55:
        return "STEADY", 0.35
    return "SPICY", 0.15


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
    window_end = now + timedelta(hours=WINDOW_HOURS)

    print("=" * 70)
    print(f"  SMART PICKS | {session.emoji} {session.name} | "
          f"window: NOW -> +{WINDOW_HOURS:.0f}h")
    print(f"  {clock_line()}")
    print("=" * 70)

    cands = OddsApiFeed(min_hours_ahead=0.0,
                        max_hours_ahead=WINDOW_HOURS,
                        markets="h2h,totals").collect()
    if not cands:
        msg = ("[FAIL] feed returned zero events - run: python doctor.py")
        print(msg)
        if tg.is_configured:
            tg.send(msg)
        return

    comparator = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0)
    pending_keys, pending_matches = _pending(logger)
    kmap = _kickoff_map()

    h2h: dict[str, dict[str, BetOpportunity]] = defaultdict(dict)
    totals: dict[str, list[BetOpportunity]] = defaultdict(list)
    supply = 0

    for sel, quotes in cands:
        opp = comparator.evaluate(sel, quotes)
        prob = opp.selection.model_probability
        if not 0.05 <= prob <= 0.95:
            continue
        ko = _kickoff_dt(kmap.get(opp.selection.match_id, ""))
        if ko is None or ko < now:
            continue
        dur = sport_duration(opp.selection.league)
        if (ko + timedelta(hours=dur)) > window_end:
            continue  # must settle within the 4h window
        supply += 1
        if opp.selection.market.upper() in ("1X2", "ML"):
            h2h[opp.selection.match_id][opp.selection.selection] = opp
        elif opp.selection.market.upper().startswith("O/U"):
            totals[opp.selection.match_id].append(opp)

    candidates: list[tuple[BetOpportunity, str]] = []
    for mid, sides in h2h.items():
        draw = sides.get("Draw")
        best = None
        for o in (sides.get("Home"), sides.get("Away")):
            if o and (best is None or
                      o.selection.model_probability >
                      best.selection.model_probability):
                best = o
        if best is None:
            continue
        cloudy = ((draw is not None and
                   draw.selection.model_probability >= 0.30) or
                  best.selection.model_probability < 0.50)
        if cloudy:
            lines = totals.get(mid, [])
            if lines:
                top = max(lines, key=lambda o: o.selection.model_probability)
                candidates.append((top, "TOTALS"))
        else:
            candidates.append((best, "WINNER"))
    for mid, lines in totals.items():
        if mid not in h2h and lines:
            top = max(lines, key=lambda o: o.selection.model_probability)
            candidates.append((top, "TOTALS"))

    seen, fresh = set(), []
    for o, lane in candidates:
        k = (o.selection.match_id, o.selection.market, o.selection.selection)
        if k in pending_keys or o.selection.match_id in pending_matches or k in seen:
            continue
        seen.add(k)
        fresh.append((o, lane))

    fresh.sort(key=lambda t: t[0].selection.model_probability, reverse=True)
    picks = fresh[:N_PICKS]

    print(f"\n  Supply: {supply} markets in-window | "
          f"{len(fresh)} fresh picks | taking {len(picks)}\n")

    if not picks:
        msg = (f"{session.emoji} {session.name}: every in-window game is "
               f"already logged. Run again in the next window for fresh 8.")
        print(msg)
        if tg.is_configured:
            tg.send(msg)
        return

    bankroll = Bankroll()
    slots = PLATFORMS[-len(picks):] if len(picks) < len(PLATFORMS) else PLATFORMS
    placed: list[tuple[str, BetOpportunity, str, str, float]] = []

    for i, (opp, lane) in enumerate(picks, start=1):
        platform = slots[i - 1]
        prob = opp.selection.model_probability
        label, mult = _tier(prob)
        eff = round(base_stake * mult, 2)
        slip = Slip(slip_type="SINGLE",
                    legs=[SlipLeg.from_opportunity(opp)],
                    stake_units=eff)
        if not bankroll.can_place(eff):
            continue
        bankroll.register_bet(eff)
        bet_id = logger.log_slip(slip, session=session.name)
        placed.append((platform, opp, bet_id, label, eff))
        leg = slip.legs[0]
        floor = round(leg.decimal_odds * 0.97, 2)
        ko = _kickoff_dt(kmap.get(leg.match_id, ""))
        done = ((ko + timedelta(hours=sport_duration(opp.selection.league)))
                .astimezone(EAT).strftime("%H:%M EAT") if ko else "?")
        msg = (f"{lane} {i}/{len(picks)} -> {platform.upper()} [{label}]\n"
               f"{leg.match_label.split(' · ')[0]}\n"
               f"KICKOFF {_kickoff_eat(kmap.get(leg.match_id, ''))} | "
               f"settles ~{done} (inside your 4h window)\n"
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
        summary = (f"{len(placed)}/8 picks | stake {total:.2f}u | "
                   f"expected {exp:+.2f}u | ALL settle by "
                   f"{(now + timedelta(hours=WINDOW_HOURS)).astimezone(EAT).strftime('%H:%M EAT')}\n"
                   f"Next window: run again any time after these finish.")
        print(summary)
        if tg.is_configured:
            tg.send(summary)


if __name__ == "__main__":
    main()