"""
smart_picks.py (FINAL) - one scanner, two lanes, decided by the odds:

  [WINNER] clear favorite (best side >= 50% consensus) -> pick the winner
  [TOTALS] tight/draw-heavy match -> PIVOT TO GOALS: the highest-probability
           O/U line.  Scoring markets do not care who wins - the professional
           answer to balanced games.

8 picks/session, 8 platforms, strict settle-inside-session (per-sport
durations), quality tiers set stakes (EDGE+ 1.0x / NEUTRAL 0.5x /
FUN 0.25x / FILLER 0.1x), dedupe-safe, Telegram per pick.
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
CACHE = Path("data") / "feed_cache.json"
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
        return f"{market.replace('O/U ', '')} {selection} (goals)"
    if m.startswith("DC"):
        return f"Double chance {selection}"
    return f"{market} -> {selection}"


def _is_suspect(o: BetOpportunity) -> bool:
    return o.decimal_odds / (1.0 / o.selection.model_probability) > SUSPECT_RATIO


def _pending_keys(logger: BetLogger):
    keys = set()
    for rec in logger.pending():
        for leg in rec.get("legs", []):
            keys.add((leg.get("match_id"), leg.get("market"), leg.get("selection")))
    return keys


def _kickoff_map() -> dict:
    out = {}
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
    base_stake = _flag(sys.argv, "--stake", 0.5)
    platforms = PLATFORMS
    session = detect()
    tg = TelegramNotifier()
    logger = BetLogger()

    now = datetime.now(timezone.utc)
    next_start = next_start_eat(now)
    hours = (next_start - now).total_seconds() / 3600.0

    cands = OddsApiFeed(min_hours_ahead=0.0, max_hours_ahead=hours,
                        markets="h2h,totals").collect()
    comparator = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0)
    pending = _pending_keys(logger)
    kmap = _kickoff_map()

    h2h: dict[str, dict[str, BetOpportunity]] = defaultdict(dict)
    totals: dict[str, list[BetOpportunity]] = defaultdict(list)
    for sel, quotes in cands:
        opp = comparator.evaluate(sel, quotes)
        if _is_suspect(opp):
            continue
        if opp.selection.market.upper() in ("1X2", "ML"):
            h2h[opp.selection.match_id][opp.selection.selection] = opp
        elif opp.selection.market.upper().startswith("O/U"):
            totals[opp.selection.match_id].append(opp)

    pool: list[tuple[BetOpportunity, str]] = []
    n_winner = n_totals = 0
    for mid, sides in h2h.items():
        ko = _kickoff_dt(kmap.get(mid, ""))
        if ko is None:
            continue
        league = sides[next(iter(sides))].selection.league
        if (ko + timedelta(hours=sport_duration(league))) > next_start:
            continue  # must settle inside this session
        home, away = sides.get("Home"), sides.get("Away")
        draw = sides.get("Draw")
        best = None
        for o in (home, away):
            if o and (best is None or
                      o.selection.model_probability > best.selection.model_probability):
                best = o
        cloudy = (draw is not None and draw.selection.model_probability >= 0.30) or (
            best is not None and best.selection.model_probability < 0.50)

        if best is not None and not cloudy and best.selection.model_probability >= 0.50 \
                and 1.10 <= best.decimal_odds <= 2.60:
            pool.append((best, "WINNER"))
            n_winner += 1
            continue
        # ---- totals pivot: tight match -> goals market ----
        lines = [o for o in totals.get(mid, [])
                 if o.selection.model_probability >= 0.60
                 and 1.05 <= o.decimal_odds <= 2.60]
        if lines:
            top = max(lines, key=lambda o: o.selection.model_probability)
            pool.append((top, "TOTALS"))
            n_totals += 1

    # matches with no h2h consensus but clean totals still qualify
    for mid, lines in totals.items():
        if mid in h2h:
            continue
        ko = _kickoff_dt(kmap.get(mid, ""))
        if ko is None:
            continue
        if not lines:
            continue
        league = lines[0].selection.league
        if (ko + timedelta(hours=sport_duration(league))) > next_start:
            continue
        good = [o for o in lines if o.selection.model_probability >= 0.60
                and 1.05 <= o.decimal_odds <= 2.60]
        if good:
            pool.append((max(good, key=lambda o: o.selection.model_probability),
                         "TOTALS"))
            n_totals += 1

    seen, dedup = set(), []
    for o, lane in pool:
        k = (o.selection.match_id, o.selection.market, o.selection.selection)
        if k in pending or k in seen:
            continue
        seen.add(k)
        dedup.append((o, lane))
    dedup.sort(key=lambda t: t[0].selection.model_probability, reverse=True)
    picks = dedup[:N_PICKS]

    print(f"[smart] pool {len(dedup)} (winner {n_winner}, totals {n_totals}) "
          f"| picks {len(picks)} | {clock_line()}")

    if not picks:
        if tg.is_configured:
            tg.send(f"{session.emoji} {session.name}: no qualifying markets "
                    f"right now - next 30-min cycle rescans.")
        return

    bankroll = Bankroll()
    for i, (o, lane) in enumerate(picks, start=1):
        platform = platforms[i - 1]
        prob = o.selection.model_probability
        label, mult = _tier(prob, o.ev_per_unit)
        eff = round(base_stake * mult, 2)
        slip = Slip(slip_type="SINGLE",
                    legs=[SlipLeg.from_opportunity(o)], stake_units=eff)
        if not bankroll.can_place(eff):
            continue
        bankroll.register_bet(eff)
        bet_id = logger.log_slip(slip, session=session.name)
        leg = slip.legs[0]
        floor = round(leg.decimal_odds * 0.97, 2)
        ko = _kickoff_dt(kmap.get(leg.match_id, ""))
        done = ((ko + timedelta(hours=sport_duration(o.selection.league)))
                .astimezone(EAT).strftime("%H:%M EAT") if ko else "?")
        reason = ("clear favorite" if lane == "WINNER"
                  else "tight match - goals market instead of the coin-flip 1X2")
        msg = (f"{lane} {session.name} {i}/{len(picks)} -> {platform.upper()} "
               f"[{label}]\n"
               f"{leg.match_label.split(' · ')[0]}\n"
               f"KICKOFF {_kickoff_eat(kmap.get(leg.match_id, ''))} | "
               f"settles ~{done}\n"
               f"PICK: {_anchor(leg.market, leg.selection, leg.match_label)}\n"
               f"Win prob {prob:.0%} | take {leg.decimal_odds:.2f} | "
               f"place if app >= {floor}\n"
               f"({reason})\n"
               f"Stake {eff:.2f}u | {bet_id}")
        print(msg)
        if tg.is_configured:
            tg.send(msg)


if __name__ == "__main__":
    main()