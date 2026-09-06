"""
single_shot.py v2 - 1 high-probability single per platform, HARD session
boundaries (every pick finishes before the next 4h session).

    python single_shot.py                        # prob >= 75%
    python single_shot.py --min-prob 0.80        # stricter
    python single_shot.py --stake 0.5
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
from utils.session import EAT, clock_line, detect, session_window

PLATFORMS: list[str] = [
    "SportyBet", "Betika", "1xBet", "BetPawa",
    "MozzartBet", "LuckyPari", "BetJam", "WekaWin",
]
SUSPECT_RATIO = 1.20
MIN_PROB = 0.75
MIN_ODDS = 1.10
MAX_ODDS = 1.60
_CACHE = Path("data") / "feed_cache.json"


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


def _flag(argv, name, default):
    if name in argv:
        i = argv.index(name)
        try:
            return float(argv[i + 1])
        except (ValueError, IndexError):
            return default
    return default


def main() -> None:
    min_prob = _flag(sys.argv, "--min-prob", MIN_PROB)
    stake = _flag(sys.argv, "--stake", 0.5)
    session = detect()
    tg = TelegramNotifier()
    logger = BetLogger()

    bound = session_window()
    print("=" * 70)
    print(f"  SINGLE SHOT v2 | {session.emoji} {session.name} | 1 pick/platform | "
          f"prob >= {min_prob:.0%}")
    print(f"  {clock_line()}")
    print("=" * 70)

    if not OddsApiFeed(min_hours_ahead=0.0, max_hours_ahead=bound).is_configured:
        print("\n  [FAIL] NO API KEYS LOADED. Run:  python doctor.py")
        return

    candidates = None
    for max_h in (bound, 12.0, 24.0, 48.0):
        cands = OddsApiFeed(min_hours_ahead=0.0, max_hours_ahead=max_h).collect()
        if cands:
            candidates = cands
            if max_h > bound:
                print(f"  [WARN] window widened to {max_h:.0f}h.")
            break
        print(f"  .. widening to {max_h:.0f}h ...")
    if not candidates:
        print("\n  [FAIL] no candidates even at 48h. Run:  python doctor.py")
        return

    comparator = OddsComparator(min_edge=0.015, min_ev_per_unit=0.015)
    opps = [comparator.evaluate(s, q) for s, q in candidates]
    clean = [o for o in opps if not _is_suspect(o)]

    by_match: dict[str, list[BetOpportunity]] = defaultdict(list)
    for o in clean:
        by_match[o.selection.match_id].append(o)

    pending = _pending_keys(logger)
    pool: list[BetOpportunity] = []
    for mid, group in by_match.items():
        best = max(group, key=lambda o: o.selection.model_probability)
        if best.selection.market.upper().startswith("SPREAD"):
            continue
        if best.selection.model_probability < min_prob:
            continue
        if not (MIN_ODDS <= best.decimal_odds <= MAX_ODDS):
            continue
        if len(best.quotes_by_book) < 3:
            continue
        k = (best.selection.match_id, best.selection.market, best.selection.selection)
        if k in pending:
            continue
        pool.append(best)

    pool.sort(key=lambda o: o.selection.model_probability, reverse=True)
    picks = pool[:len(platforms)]

    if not picks:
        print(f"\n  No picks >= {min_prob:.0%} +EV in this session window.")
        print("  Loosen:  python single_shot.py --min-prob 0.70")
        if tg.is_configured:
            tg.send(f"SINGLE SHOT {session.name}: no picks >= {min_prob:.0%} +EV.")
        return

    bankroll = Bankroll()
    kmap = _kickoff_map()
    placed = []
    print(f"\n  Pool: {len(pool)} qualifying (showing {len(picks)}):")
    for i, o in enumerate(picks, start=1):
        platform = platforms[i - 1]
        ev = o.ev_per_unit
        grade = "EDGE+" if ev >= 0.02 else "NEUTRAL"
        eff = stake if grade == "EDGE+" else stake * 0.5
        slip = Slip(slip_type="SINGLE",
                    legs=[SlipLeg.from_opportunity(o)],
                    stake_units=eff)
        if not bankroll.can_place(eff):
            print(f"  !! exposure cap blocked {platform}.")
            continue
        bankroll.register_bet(eff)
        bet_id = logger.log_slip(slip, session=session.name)
        placed.append((platform, o, bet_id, grade, eff))
        leg = slip.legs[0]
        floor = round(leg.decimal_odds * 0.97, 2)
        print(f"  {i}. [{grade}] {platform.upper()}")
        print(f"     MATCH  : {leg.match_label.split(' · ')[0]}")
        print(f"     KICKOFF: {_kickoff_eat(kmap.get(leg.match_id, ''))}")
        print(f"     PICK   : {_anchor(leg.market, leg.selection, leg.match_label)}")
        print(f"     PRICE  : take {leg.decimal_odds:.2f} | place if app >= {floor}")
        print(f"     MODEL  : {leg.model_prob:.0%} win | EV {ev * 100:+.1f}% | "
              f"stake {eff:.2f}u | {bet_id}")
        if tg.is_configured:
            tg.send(
                f"SINGLE {session.name} {i}/{len(picks)} -> {platform.upper()} "
                f"[{grade}]\n"
                f"{leg.match_label.split(' · ')[0]}\n"
                f"KICKOFF {_kickoff_eat(kmap.get(leg.match_id, ''))}\n"
                f"PICK: {_anchor(leg.market, leg.selection, leg.match_label)}\n"
                f"Win prob {leg.model_prob:.0%} | take {leg.decimal_odds:.2f} | "
                f"place if app >= {floor}\n"
                f"Stake {eff:.2f}u | {bet_id}"
            )

    if placed:
        exp = sum(o.ev_per_unit * e for _, o, _, _, e in placed)
        avg_p = sum(o.selection.model_probability for _, o, _, _, _ in placed) / len(placed)
        msg = (f"SINGLE SHOT {session.name}: {len(placed)} picks, "
               f"avg win prob {avg_p:.0%}, expected {exp:+.2f}u, "
               f"stake {sum(e for _, _, _, _, e in placed):.2f}u. "
               f"Auto-settle: python settle.py")
        print("\n" + msg)
        if tg.is_configured:
            tg.send(msg)
    print("\n  Settle:  python settle.py")


if __name__ == "__main__":
    main()