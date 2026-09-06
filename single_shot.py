"""
single_shot.py v3 - GUARANTEED 8 picks per session, quality-graded stakes.

Non-negotiable: 8 bets, 8 platforms, every session.  The odds calculation
sets the STAKE, never the absence:

    EDGE+   prob >= 70% and EV >= +2%     -> 1.00x stake
    NEUTRAL prob >= 60% and EV >= -2%     -> 0.50x stake
    FUN     prob >= 50%                   -> 0.25x stake
    FILLER  best remaining (sanity >= 45%) -> 0.10x stake

Session-bound first: picks must finish before the next 4h session.  Only
if the window supplies fewer than 8 does it widen (+4h steps), and every
widened pick is labeled CROSS (finishes in the next window) - you see it,
you decide.  One pick per match, team-anchored, no spread ambiguity.

    python single_shot.py                     # 75% dial for tier-1
    python single_shot.py --min-prob 0.80
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
N_PICKS = 8
MIN_ODDS = 1.10
MAX_ODDS = 1.80          # singles product: short prices only
SANITY_PROB = 0.45       # never emit anything below this
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


def _tier(prob: float, ev: float, tier1_prob: float) -> tuple[str, float]:
    """(label, stake multiplier) from the odds calculation."""
    if prob >= tier1_prob and ev >= 0.02:
        return "EDGE+", 1.00
    if prob >= 0.60 and ev >= -0.02:
        return "NEUTRAL", 0.50
    if prob >= 0.50:
        return "FUN", 0.25
    return "FILLER", 0.10


def _clean_pool(cands, comparator, pending) -> list[BetOpportunity]:
    """Best non-spread pick per match, sanity-filtered."""
    opps = [comparator.evaluate(s, q) for s, q in cands]
    opps = [o for o in opps if not _is_suspect(o)]
    by_match: dict[str, list[BetOpportunity]] = defaultdict(list)
    for o in opps:
        by_match[o.selection.match_id].append(o)
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
        pool.append(best)
    return pool


def main() -> None:
    tier1_prob = _flag(sys.argv, "--min-prob", 0.75)
    base_stake = _flag(sys.argv, "--stake", 0.5)
    platforms = PLATFORMS
    session = detect()
    tg = TelegramNotifier()
    logger = BetLogger()

    bound = session_window()
    print("=" * 70)
    print(f"  SINGLE SHOT v3 | {session.emoji} {session.name} | "
          f"GUARANTEED {N_PICKS} picks (stakes graded by quality)")
    print(f"  {clock_line()}")
    print("=" * 70)

    if not OddsApiFeed(min_hours_ahead=0.0, max_hours_ahead=bound).is_configured:
        print("\n  [FAIL] NO API KEYS LOADED. Run:  python doctor.py")
        return

    comparator = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0)
    pending = _pending_keys(logger)
    kmap = _kickoff_map()

    # 1) in-window picks first (session-pure)
    pool: list[BetOpportunity] = []
    for max_h in (bound, 12.0, 24.0, 48.0):
        cands = OddsApiFeed(min_hours_ahead=0.0, max_hours_ahead=max_h).collect()
        pool = _clean_pool(cands, comparator, pending)
        if len(pool) >= N_PICKS:
            break
        print(f"  .. {len(pool)}/8 in-window - widening to {max_h:.0f}h ...")

    pool.sort(key=lambda o: o.selection.model_probability, reverse=True)
    picks = pool[:N_PICKS]

    if not picks:
        print("\n  [FAIL] not one priceable game found even at 48h. "
              "Run:  python doctor.py")
        return

    bankroll = Bankroll()
    placed = []
    print(f"\n  Picking top {len(picks)} by win probability:\n")
    for i, o in enumerate(picks, start=1):
        platform = platforms[i - 1]
        label, mult = _tier(o.selection.model_probability, o.ev_per_unit,
                            tier1_prob)
        eff = round(base_stake * mult, 2)
        in_window = session_window() >= _hours_until_kickoff(o, kmap)
        tag = "" if in_window else "  [CROSS - finishes next window]"
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
        print(f"  {i}. [{label:<7}] {platform.upper()}{tag}")
        print(f"     MATCH  : {leg.match_label.split(' · ')[0]}")
        print(f"     KICKOFF: {_kickoff_eat(kmap.get(leg.match_id, ''))}")
        print(f"     PICK   : {_anchor(leg.market, leg.selection, leg.match_label)}")
        print(f"     PRICE  : take {leg.decimal_odds:.2f} | "
              f"place if app >= {floor}")
        print(f"     MODEL  : {leg.model_prob:.0%} win | EV {o.ev_per_unit * 100:+.1f}% "
              f"| stake {eff:.2f}u | {bet_id}\n")
        if tg.is_configured:
            tg.send(
                f"SINGLE {session.name} {i}/{len(picks)} -> {platform.upper()} "
                f"[{label}]{tag}\n"
                f"{leg.match_label.split(' · ')[0]}\n"
                f"KICKOFF {_kickoff_eat(kmap.get(leg.match_id, ''))}\n"
                f"PICK: {_anchor(leg.market, leg.selection, leg.match_label)}\n"
                f"Win prob {leg.model_prob:.0%} | take {leg.decimal_odds:.2f} | "
                f"place if app >= {floor}\n"
                f"Stake {eff:.2f}u | {bet_id}"
            )

    if placed:
        exp = sum(o.ev_per_unit * e for _, o, _, _, e in placed)
        avg_p = sum(o.selection.model_probability
                    for _, o, _, _, _ in placed) / len(placed)
        tiers = defaultdict(int)
        for _, _, _, l, _ in placed:
            tiers[l] += 1
        tier_line = ", ".join(f"{k}:{v}" for k, v in sorted(tiers.items()))
        msg = (
            f"SINGLE SHOT {session.name}: {len(placed)}/8 picks "
            f"[{tier_line}], avg win prob {avg_p:.0%}, "
            f"expected P/L {exp:+.2f}u, "
            f"stake {sum(e for _, _, _, _, e in placed):.2f}u total.\n"
            f"Stake size = confidence. FILLER picks are lottery-priced "
            f"on purpose - 0.1u each.\n"
            f"Auto-settle: python settle.py"
        )
        print(msg)
        if tg.is_configured:
            tg.send(msg)
    print("\n  Settle:  python settle.py")


def _hours_until_kickoff(o: BetOpportunity, kmap: dict) -> float:
    ts = kmap.get(o.selection.match_id)
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return (dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return 0.0


if __name__ == "__main__":
    main()