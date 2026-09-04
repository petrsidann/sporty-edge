"""
squad.py - SQUAD mode: 3 platform-tagged slips x 4 legs, disjoint matches.

    python squad.py            (default 0.5u per slip)
    python squad.py --stake 0.25

Never stands down while any game is listed worldwide: the kickoff window
widens 6 -> 12 -> 24 -> 48h until picks exist, and if every fetch fails it
falls back to the last cached snapshot (marked STALE - verify prices).
Every sheet prints the TRUE win probability beside the combined odds.
Slips are logged (session SQUAD) so settle.py tracks them.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import replace as dc_replace

from config.settings import ACTION_SETTINGS, FEED_SETTINGS, PLACEABLE_BOOKS
from feeds.oddsapi import OddsApiFeed
from notify.telegram import TelegramNotifier
from odds.comparator import BetOpportunity, OddsComparator
from slips.generator import Slip, SlipLeg
from slips.platform_slip import choose_platform
from utils.bankroll import Bankroll
from utils.logger import BetLogger
from utils.session import detect

SUSPECT_RATIO = 1.20
PLATFORMS = (PLACEABLE_BOOKS[:3] if len(PLACEABLE_BOOKS) >= 3
             else ["SportyBet", "Betika", "BetPawa"])
LEGS_PER_SLIP = 4
MIN_ODDS = ACTION_SETTINGS.min_odds
MAX_ODDS = ACTION_SETTINGS.max_odds
MIN_PROB = ACTION_SETTINGS.min_prob


def _trunc(t: str, w: int) -> str:
    return t if len(t) <= w else t[: w - 1] + "..."


def _is_suspect(o: BetOpportunity) -> bool:
    return o.decimal_odds / (1.0 / o.selection.model_probability) > SUSPECT_RATIO


def _drop_degenerate(opps: list[BetOpportunity]) -> list[BetOpportunity]:
    groups: dict[tuple[str, str], int] = defaultdict(int)
    for o in opps:
        if o.edge > 0.0:
            groups[(o.selection.match_id, o.selection.market)] += 1
    bad = {k for k, n in groups.items() if n > 1}
    return [o for o in opps
            if (o.selection.match_id, o.selection.market) not in bad]


def _pending_keys(logger: BetLogger) -> set[tuple[str, str, str]]:
    keys = set()
    for rec in logger.pending():
        for leg in rec.get("legs", []):
            keys.add((leg.get("match_id"), leg.get("market"), leg.get("selection")))
    return keys


def main() -> None:
    stake = 0.5
    if "--stake" in sys.argv:
        i = sys.argv.index("--stake")
        try:
            stake = float(sys.argv[i + 1])
        except (ValueError, IndexError):
            pass

    session = detect()
    tg = TelegramNotifier()
    logger = BetLogger()
    print("=" * 66)
    print(f"  SQUAD  |  {session.emoji} {session.name}  |  "
          f"{len(PLATFORMS)} slips x {LEGS_PER_SLIP} legs @ {stake}u each")
    print("=" * 66)

    # 1) data: widen window until picks exist
    candidates = None
    for max_h in (6.0, 12.0, 24.0, 48.0):
        feed = OddsApiFeed(min_hours_ahead=0.25, max_hours_ahead=max_h)
        cands = feed.collect()
        if cands:
            candidates = cands
            break
        print(f"  .. nothing inside {max_h:.0f}h - widening ...")
    if not candidates:
        print("  Feed down and no cache available. Check keys/credits and retry.")
        if tg.is_configured:
            tg.send("SQUAD: no data source (keys/credits?) - fix ODDS_API_KEYS.")
        return

    # 2) guards
    comparator = OddsComparator(min_edge=FEED_SETTINGS.min_edge_feed,
                                min_ev_per_unit=FEED_SETTINGS.min_ev_feed)
    opps = [comparator.evaluate(s, q) for s, q in candidates]
    clean = [o for o in opps if not _is_suspect(o)]
    clean = _drop_degenerate(clean)
    clean = [o for o in clean
             if MIN_ODDS <= o.decimal_odds <= MAX_ODDS
             and o.selection.model_probability >= MIN_PROB
             and len(o.quotes_by_book) >= 2]

    value = comparator.rank([o for o in clean if comparator.is_value(o)])
    vkeys = {(o.selection.match_id, o.selection.market, o.selection.selection)
             for o in value}
    rest = sorted((o for o in clean
                   if (o.selection.match_id, o.selection.market,
                       o.selection.selection) not in vkeys),
                  key=lambda o: (o.selection.model_probability, o.edge),
                  reverse=True)
    ranked = value + rest

    # 3) dedupe vs ledger, pick disjoint top 12
    pending = _pending_keys(logger)
    picks: list[BetOpportunity] = []
    used: set[str] = set()
    for o in ranked:
        if len(picks) >= len(PLATFORMS) * LEGS_PER_SLIP:
            break
        k = (o.selection.match_id, o.selection.market, o.selection.selection)
        if o.selection.match_id in used or k in pending:
            continue
        picks.append(o)
        used.add(o.selection.match_id)

    if not picks:
        print("  Every candidate pick is already PENDING - nothing new. "
              "Delete data/feed_cache.json to force fresh prices.")
        return

    # 4) build the squad: round-robin picks across platforms
    bankroll = Bankroll()
    slips: list[Slip] = []
    for i, platform in enumerate(PLATFORMS):
        legs = picks[i::len(PLATFORMS)][:LEGS_PER_SLIP]
        if not legs:
            continue
        slips.append(Slip(slip_type="SQUAD", legs=[SlipLeg.from_opportunity(l)
                                                   for l in legs],
                          stake_units=stake))

    print(f"\n  Squad pool: {len(picks)} picks -> {len(slips)} slips")
    for slip in slips:
        if not bankroll.can_place(slip.stake_units):
            print(f"  !! exposure cap blocked one slip - skip or lower stakes.")
            continue
        bankroll.register_bet(slip.stake_units)
        bet_id = logger.log_slip(slip, session=session.name)
        sheet = choose_platform(slip)
        target = PLATFORMS[slips.index(slip) % len(PLATFORMS)]
        if sheet is not None:
            sheet = dc_replace(sheet, platform=target)
            text = sheet.render(bet_id=bet_id)
        else:
            text = slip.render()
        print()
        print(text)
        print(f"  -> logged as {bet_id} (PENDING, {session.name})")
        if tg.is_configured:
            tg.send(f"SQUAD {slips.index(slip) + 1}/{len(slips)} "
                    f"-> {target.upper()}\n\n{text}")

    if tg.is_configured:
        tot = sum(s.stake_units for s in slips)
        exp = sum(s.stake_units * s.ev_per_unit for s in slips)
        tg.send(f"SQUAD summary: {len(slips)} slips, {tot:.2f}u staked, "
                f"expected P/L {exp:+.2f}u. Different games on each platform "
                f"- never duplicate a pick across platforms.")

    print("\n  RULE: each pick appears on ONE platform only.")
    print("  Settle everything later:  python settle.py")


if __name__ == "__main__":
    main()