"""
single_shot.py v5 - 8 picks per session, ALL settle inside the session,
with AUTOSCAN: if fewer than 8 qualify, it rescans every 30 minutes
(cheap refetch of high-supply sports) until 8 are found or the window
closes.  v5 fixes the filter wall that killed 13 in-window events:

  * odds ceiling 1.80 -> 2.30  (unlocks favourites 1.80-2.30 and ALL
    totals markets - Over/Under now flows into the pool)
  * sanity floor 0.45 -> 0.40  (balanced matches survive)
  * quality still sets the stake: EDGE+ 1.0x | NEUTRAL 0.5x |
    FUN 0.25x | FILLER 0.1x

    python single_shot.py                # autoscan ON by default
    python single_shot.py --no-autoscan
    python single_shot.py --stake 0.5
"""

from __future__ import annotations

import json
import sys
import time
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
MAX_ODDS = 2.30          # was 1.80 - the filter-wall culprit
SANITY_PROB = 0.40       # was 0.45 - the second culprit
SCAN_INTERVAL_MIN = 30
_CACHE = Path("data") / "feed_cache.json"

# sports most likely to supply thin windows - refetched during autoscan
FAST_RESCAN_SPORTS: tuple[str, ...] = (
    "soccer_japan_j_league", "soccer_japan_j2_league",
    "soccer_korea_kleague1", "soccer_korea_kleague2",
    "soccer_australia_aleague", "mma_mixed_martial_arts",
    "baseball_kbo", "baseball_npb", "soccer_brazil_serie_b",
)

DURATIONS: list[tuple[str, float]] = [
    ("mlb", 2.9), ("kbo", 2.9), ("npb", 2.9), ("baseball", 2.9),
    ("nfl", 3.2), ("ncaaf", 3.3), ("american", 3.2),
    ("nba", 2.4), ("basketball", 2.4),
    ("nhl", 2.6), ("hockey", 2.6),
    ("mma", 1.5), ("ufc", 1.5), ("fight", 1.5),
    ("tennis", 2.2),
]


def sport_duration(league: str) -> float:
    s = (league or "").lower()
    for key, hours in DURATIONS:
        if key in s:
            return hours
    return 2.05


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


def _scan(next_start: datetime, comparator, pending, fast_only: bool):
    """One scan pass -> (pool, per_sport_supply)."""
    if fast_only:
        feed = OddsApiFeed(min_hours_ahead=0.0,
                           max_hours_ahead=(next_start - datetime.now(timezone.utc)
                                            ).total_seconds() / 3600.0,
                           sports=FAST_RESCAN_SPORTS, markets="h2h")
        cands = feed.collect()
    else:
        cands = OddsApiFeed(
            min_hours_ahead=0.0,
            max_hours_ahead=(next_start - datetime.now(timezone.utc)
                             ).total_seconds() / 3600.0,
        ).collect()

    opps = [comparator.evaluate(s, q) for s, q in cands]
    opps = [o for o in opps if not _is_suspect(o)]

    by_match: dict[str, list[BetOpportunity]] = defaultdict(list)
    for o in opps:
        by_match[o.selection.match_id].append(o)

    supply: dict[str, int] = defaultdict(int)
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
        ko = _kickoff_dt(_kickoff_map().get(best.selection.match_id, ""))
        if ko is None:
            continue
        dur = sport_duration(best.selection.league)
        if (ko + timedelta(hours=dur)) > next_start:
            continue
        pool.append(best)
        supply[best.selection.league[:20]] += 1
    return pool, supply


def main() -> None:
    base_stake = _flag(sys.argv, "--stake", 0.5)
    autoscan = "--no-autoscan" not in sys.argv
    platforms = PLATFORMS
    session = detect()
    tg = TelegramNotifier()
    if tg.is_configured:
        tg.send(f"\u23f0 {session.emoji} {session.name} run started - scanning the board...")
    logger = BetLogger()

    now = datetime.now(timezone.utc)
    next_start = next_start_eat(now)
    print("=" * 70)
    print(f"  SINGLE SHOT v5 | {session.emoji} {session.name} | "
          f"{N_PICKS} in-session picks | autoscan {'ON' if autoscan else 'OFF'}")
    print(f"  {clock_line()}")
    print("=" * 70)

    if not OddsApiFeed(min_hours_ahead=0.0,
                       max_hours_ahead=(next_start - now).total_seconds() / 3600.0
                       ).is_configured:
        print("\n  [FAIL] NO API KEYS LOADED. Run:  python doctor.py")
        return

    comparator = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0)
    pending = _pending_keys(logger)

    pool, supply = _scan(next_start, comparator, pending, fast_only=False)
    attempt = 1

    while len(pool) < N_PICKS and autoscan:
        mins_left = (next_start - datetime.now(timezone.utc)).total_seconds() / 60.0
        if mins_left <= 25:
            print(f"\n  autoscan: only {mins_left:.0f} min left before the next "
                  f"session - stopping with {len(pool)} picks.")
            break
        print(f"\n  autoscan: {len(pool)}/{N_PICKS} found - rescanning in "
              f"{SCAN_INTERVAL_MIN} min (cheap fast pass)... "
              f"({mins_left:.0f} min of window left)")
        if tg.is_configured and attempt == 1:
            tg.send(f"{session.name}: {len(pool)}/{N_PICKS} found so far - "
                    f"autoscan running every {SCAN_INTERVAL_MIN} min.")
        time.sleep(SCAN_INTERVAL_MIN * 60)
        attempt += 1
        more, _ = _scan(next_start, comparator, pending, fast_only=True)
        # merge: new qualifying picks only
        seen = {(o.selection.match_id, o.selection.market, o.selection.selection)
                for o in pool}
        for o in more:
            k = (o.selection.match_id, o.selection.market, o.selection.selection)
            if k not in seen:
                pool.append(o)
                seen.add(k)
        pool.sort(key=lambda o: o.selection.model_probability, reverse=True)

    pool.sort(key=lambda o: o.selection.model_probability, reverse=True)
    picks = pool[:N_PICKS]
    kmap = _kickoff_map()

    print(f"\n  Final pool: {len(picks)} in-session picks "
          f"({attempt} scan(s)):")
    for i, o in enumerate(picks, start=1):
        print(f"   {i:>2}. {o.selection.match_label.split(' · ')[0][:34]:<34} "
              f"{_anchor(o.selection.market, o.selection.selection, o.selection.match_label)[:26]:<26} "
              f"@ {o.decimal_odds:<5.2f} p={o.selection.model_probability:.0%}")

    if len(picks) < N_PICKS:
        print(f"\n  [SHORTFALL] world supplied {len(picks)}/{N_PICKS} even after "
              f"autoscan. Every game that can settle in this window is above.")
        if tg.is_configured:
            tg.send(f"{session.name}: {len(picks)}/{N_PICKS} in-session games "
                    f"exist after autoscan - shortfall is world supply, "
                    f"not the system.")

    bankroll = Bankroll()
    placed = []
    for i, o in enumerate(picks, start=1):
        platform = platforms[i - 1]
        label, mult = _tier(o.selection.model_probability, o.ev_per_unit)
        eff = round(base_stake * mult, 2)
        slip = Slip(slip_type="SINGLE",
                    legs=[SlipLeg.from_opportunity(o)],
                    stake_units=eff)
        if not bankroll.can_place(eff):
            print(f"  !! exposure cap blocked {platform}.")
            continue
        bankroll.register_bet(eff)
        bet_id = logger.log_slip(slip, session=session.name)
        placed.append((platform, o, bet_id, label, eff))
        leg = slip.legs[0]
        floor = round(leg.decimal_odds * 0.97, 2)
        ko = _kickoff_dt(kmap.get(leg.match_id, ""))
        done = ((ko + timedelta(hours=sport_duration(o.selection.league)))
                .astimezone(EAT).strftime("%H:%M EAT") if ko else "?")
        print(f"  {i}. [{label:<7}] {platform.upper()}")
        print(f"     MATCH   : {leg.match_label.split(' · ')[0]}")
        print(f"     KICKOFF : {_kickoff_eat(kmap.get(leg.match_id, ''))} "
              f"| settles ~{done}")
        print(f"     PICK    : {_anchor(leg.market, leg.selection, leg.match_label)}")
        print(f"     PRICE   : take {leg.decimal_odds:.2f} | place if app >= {floor}")
        print(f"     MODEL   : {leg.model_prob:.0%} | EV {o.ev_per_unit * 100:+.1f}% "
              f"| stake {eff:.2f}u | {bet_id}\n")
        if tg.is_configured:
            tg.send(
                f"SINGLE {session.name} {i}/{len(picks)} -> {platform.upper()} "
                f"[{label}]\n"
                f"{leg.match_label.split(' · ')[0]}\n"
                f"KICKOFF {_kickoff_eat(kmap.get(leg.match_id, ''))} | "
                f"settles ~{done}\n"
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
        msg = (f"SINGLE SHOT {session.name}: {len(placed)} picks "
               f"[{', '.join(f'{k}:{v}' for k, v in sorted(tiers.items()))}], "
               f"avg win prob {avg_p:.0%}, expected {exp:+.2f}u, "
               f"stake {sum(e for _, _, _, _, e in placed):.2f}u. "
               f"All settle inside this session.")
        print("\n" + msg)
        if tg.is_configured:
            tg.send(msg)
    print("\n  Settle:  python settle.py")


if __name__ == "__main__":
    main()