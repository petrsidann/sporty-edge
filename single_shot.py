"""
single_shot.py v4 - 8 picks per session, ALL inside the session timeline.

Rules baked in:
  * A pick qualifies only if: kickoff >= now AND kickoff + sport_duration
    <= next session start.  Per-sport durations (soccer ~2h, fights ~1.5h,
    MLB ~2.9h...) - so every pick is PLAYED AND SETTLED inside its session.
  * NO cross-session picks.  If the world supplies fewer than 8 in-window
    games, you get the max + a shortfall report naming the gaps.
  * Quality sets the stake: EDGE+ 1.0x | NEUTRAL 0.5x | FUN 0.25x |
    FILLER 0.1x.  8 bets, 8 platforms, one pick per match, team-anchored.

Run at session start (08:00 / 12:00 / 16:00 / 20:00 / 00:00 / 04:00 EAT)
for the full window.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
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
MAX_ODDS = 1.80
SANITY_PROB = 0.45
_CACHE = Path("data") / "feed_cache.json"

# expected game duration (hours) by sport - used to guarantee every pick
# FINISHES before the next session starts
DURATIONS: list[tuple[str, float]] = [
    ("mlb", 2.9), ("kbo", 2.9), ("npb", 2.9), ("baseball", 2.9),
    ("nfl", 3.2), ("ncaaf", 3.3), ("american", 3.2),
    ("nba", 2.4), ("basketball", 2.4),
    ("nhl", 2.6), ("hockey", 2.6),
    ("mma", 1.5), ("ufc", 1.5), ("fight", 1.5),
    ("tennis", 2.2),
]


def sport_duration(league: str) -> float:
    """Expected finish duration for this league/sport, in hours."""
    s = (league or "").lower()
    for key, hours in DURATIONS:
        if key in s:
            return hours
    return 2.05  # soccer default


def _teams(label: str) -> tuple[str, str]:
    left, _, right = label.partition(" vs ")
    away = right.split(" · ")[0].strip()
    return left.strip(), away


def _anchor(market: str, selection: str, label: str) -> str:
    home, away = _teams(label)
    m = market.upper()
    if m.startswith(("ML", "MONEYLINE", "1X2")):
        if selection == "Home":
            return f"{home} to win"
        if selection == "Away":
            return f"{away} to win"
        return "Draw"
    if m.startswith("DC"):
        return f"Double chance {selection}"
    if m.startswith("O/U"):
        return f"{market.replace('O/U ', '')} {selection} (match total)"
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
    base_stake = _flag(sys.argv, "--stake", 0.5)
    platforms = PLATFORMS
    session = detect()
    tg = TelegramNotifier()
    logger = BetLogger()

    now = datetime.now(timezone.utc)
    next_start = next_start_eat(now)
    print("=" * 70)
    print(f"  SINGLE SHOT v4 | {session.emoji} {session.name} | "
          f"{N_PICKS} in-session picks, 8 platforms")
    print(f"  {clock_line()}")
    print("=" * 70)

    if not OddsApiFeed(min_hours_ahead=0.0,
                       max_hours_ahead=(next_start - now).total_seconds() / 3600.0
                       ).is_configured:
        print("\n  [FAIL] NO API KEYS LOADED. Run:  python doctor.py")
        return

    # fetch the whole remaining session block, then filter per-sport
    cands = OddsApiFeed(
        min_hours_ahead=0.0,
        max_hours_ahead=(next_start - now).total_seconds() / 3600.0,
    ).collect()

    comparator = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0)
    pending = _pending_keys(logger)
    kmap = _kickoff_map()

    opps = [comparator.evaluate(s, q) for s, q in cands]
    opps = [o for o in opps if not _is_suspect(o)]

    # one best pick per match
    by_match: dict[str, list[BetOpportunity]] = defaultdict(list)
    for o in opps:
        by_match[o.selection.match_id].append(o)

    per_sport_supply: dict[str, int] = defaultdict(int)
    pool: list[BetOpportunity] = []
    for group in by_match.values():
        best = max(group, key=lambda o: o.selection.model_probability)
        if best.selection.market.upper().startswith("SPREAD"):
            continue
        if not (MIN_ODDS <= best.decimal_odds <= MAX_ODDS):
            continue
        if best.selection.model_probability < SANITY_PROB:
            continue
        if len(best.quotes_by_book) < 2:
            continue
        k = (best.selection.match_id, best.selection.market,
             best.selection.selection)
        if k in pending:
            continue
        # STRICT in-session check: kickoff + sport duration <= next session
        ko = _kickoff_dt(kmap.get(best.selection.match_id, ""))
        if ko is None:
            continue
        dur = sport_duration(best.selection.league)
        if (ko + __import__("datetime").timedelta(hours=dur)) > next_start:
            continue
        pool.append(best)
        per_sport_supply[best.selection.league[:20]] += 1

    pool.sort(key=lambda o: o.selection.model_probability, reverse=True)
    picks = pool[:N_PICKS]

    print(f"\n  In-session supply by sport: "
          + (", ".join(f"{k}:{v}" for k, v in
                       sorted(per_sport_supply.items(), key=lambda x: -x[1]))
             or "none"))

    if len(picks) < N_PICKS:
        print(
            f"\n  [SHORTFALL] world supplied {len(picks)}/{N_PICKS} games that "
            f"finish before the next session."
        )
        print("  Options: (a) accept fewer picks this session,")
        print("           (b) run python single_shot.py at the NEXT session start.")
        if tg.is_configured:
            tg.send(
                f"SINGLE SHOT {session.name}: {len(picks)}/{N_PICKS} in-session "
                f"games exist right now - shortfall report, no cross picks."
            )
        if not picks:
            return

    bankroll = Bankroll()
    placed = []
    print(f"\n  Picks ({len(picks)}), best probability first:\n")
    for i, o in enumerate(picks, start=1):
        platform = platforms[i - 1]
        label, mult = _tier(o.selection.model_probability, o.ev_per_unit)
        eff = round(base_stake * mult, 2)
        slip = Slip(slip_type="SINGLE",
                    legs=[SlipLeg.from_opportunity(o)],
                    stake_units=eff)
        if not bankroll.can_place(eff):
            print(f"  !! exposure cap blocked {platform} - settle pending first.")
            continue
        bankroll.register_bet(eff)
        bet_id = logger.log_slip(slip, session=session.name)
        placed.append((platform, o, bet_id, label, eff))
        leg = slip.legs[0]
        floor = round(leg.decimal_odds * 0.97, 2)
        dur = sport_duration(o.selection.league)
        ko = _kickoff_dt(kmap.get(leg.match_id, ""))
        done_eat = (
            (ko + __import__("datetime").timedelta(hours=dur))
            .astimezone(EAT).strftime("%H:%M EAT")
            if ko else "?"
        )
        print(f"  {i}. [{label:<7}] {platform.upper()}")
        print(f"     MATCH   : {leg.match_label.split(' · ')[0]}")
        print(f"     KICKOFF : {_kickoff_eat(kmap.get(leg.match_id, ''))} "
              f"| settles ~{done_eat}")
        print(f"     PICK    : {_anchor(leg.market, leg.selection, leg.match_label)}")
        print(f"     PRICE   : take {leg.decimal_odds:.2f} | place if app >= {floor}")
        print(f"     MODEL   : {leg.model_prob:.0%} win | "
              f"EV {o.ev_per_unit * 100:+.1f}% | stake {eff:.2f}u | {bet_id}\n")
        if tg.is_configured:
            tg.send(
                f"SINGLE {session.name} {i}/{len(picks)} -> {platform.upper()} "
                f"[{label}]\n"
                f"{leg.match_label.split(' · ')[0]}\n"
                f"KICKOFF {_kickoff_eat(kmap.get(leg.match_id, ''))} | "
                f"settles ~{done_eat}\n"
                f"PICK: {_anchor(leg.market, leg.selection, leg.match_label)}\n"
                f"Win prob {leg.model_prob:.0%} | take {leg.decimal_odds:.2f} | "
                f"place if app >= {floor}\n"
                f"Stake {eff:.2f}u | {bet_id}"
            )

    if placed:
        exp = sum(o.ev_per_unit * e for _, o, _, _, e in placed)
        avg_p = sum(o.selection.model_probability
                    for _, o, _, _, _ in placed) / len(placed)
        tiers: dict[str, int] = defaultdict(int)
        for _, _, _, l, _ in placed:
            tiers[l] += 1
        msg = (
            f"SINGLE SHOT {session.name}: {len(placed)} in-session picks "
            f"[{', '.join(f'{k}:{v}' for k, v in sorted(tiers.items()))}], "
            f"avg win prob {avg_p:.0%}, expected {exp:+.2f}u, "
            f"stake {sum(e for _, _, _, _, e in placed):.2f}u. "
            f"All settle inside this session."
        )
        print("\n" + msg)
        if tg.is_configured:
            tg.send(msg)
    print("\n  Settle:  python settle.py")


if __name__ == "__main__":
    main()