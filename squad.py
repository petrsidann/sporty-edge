"""
squad.py v2 - platform-tagged slips x 4 legs, disjoint matches,
picks ORDERED BY KICKOFF (soonest first). Never claims 'no games' when
the real problem is missing keys - it says exactly what is wrong.

    python squad.py                # 3 slips @ 0.5u each
    python squad.py --stake 0.25
    python squad.py --slips 4      # one slip per platform
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import replace as dc_replace
from pathlib import Path

from config.settings import ACTION_SETTINGS, PLACEABLE_BOOKS
from feeds.oddsapi import OddsApiFeed
from notify.telegram import TelegramNotifier
from odds.comparator import BetOpportunity, OddsComparator
from slips.generator import Slip, SlipLeg
from slips.platform_slip import choose_platform
from utils.bankroll import Bankroll
from utils.logger import BetLogger
from utils.session import clock_line, detect

SUSPECT_RATIO = 1.20
LEGS_PER_SLIP = 4
MIN_ODDS = ACTION_SETTINGS.min_odds
MAX_ODDS = ACTION_SETTINGS.max_odds
MIN_PROB = ACTION_SETTINGS.min_prob
_CACHE = Path("data") / "feed_cache.json"


def _trunc(t: str, w: int) -> str:
    return t if len(t) <= w else t[: w - 1] + "..."


def _is_suspect(o: BetOpportunity) -> bool:
    return o.decimal_odds / (1.0 / o.selection.model_probability) > SUSPECT_RATIO


def _drop_degenerate(opps):
    groups = defaultdict(int)
    for o in opps:
        if o.edge > 0.0:
            groups[(o.selection.match_id, o.selection.market)] += 1
    bad = {k for k, n in groups.items() if n > 1}
    return [o for o in opps
            if (o.selection.match_id, o.selection.market) not in bad]


def _pending_keys(logger: BetLogger):
    keys = set()
    for rec in logger.pending():
        for leg in rec.get("legs", []):
            keys.add((leg.get("match_id"), leg.get("market"), leg.get("selection")))
    return keys


def _kickoff_map() -> dict:
    """event id prefix -> commence_time, read free from the feed cache."""
    out = {}
    try:
        data = json.loads(_CACHE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return out
    for entry in (data.get("sports") or {}).values():
        for ev in entry.get("events") or []:
            eid = str(ev.get("id") or "")[:10].upper()
            ts = str(ev.get("commence_time") or "")
            if eid and ts:
                out[eid] = ts
    return out


def _flag(argv, name, default):
    if name in argv:
        i = argv.index(name)
        try:
            return float(argv[i + 1]) if default.__class__ is float else int(argv[i + 1])
        except (ValueError, IndexError):
            return default
    return default


def main() -> None:
    stake = _flag(sys.argv, "--stake", 0.5)
    n_slips = int(_flag(sys.argv, "--slips", 3))
    n_slips = max(1, min(n_slips, len(PLACEABLE_BOOKS)))
    platforms = list(PLACEABLE_BOOKS[:n_slips])
    session = detect()
    tg = TelegramNotifier()
    logger = BetLogger()

    print("=" * 66)
    print(f"  SQUAD | {session.emoji} {session.name} | {len(platforms)} slips x "
          f"{LEGS_PER_SLIP} legs @ {stake}u")
    print(f"  {clock_line()}")
    print(f"  Load targets: {', '.join(p.upper() for p in platforms)}")
    print("=" * 66)

    if "--refresh" not in sys.argv:
        feed = OddsApiFeed(min_hours_ahead=0.25, max_hours_ahead=6.0)
        if not feed.is_configured:
            print()
            print("  [FAIL] NO API KEYS LOADED - the feed never ran. That is")
            print("  why you saw 'no games': the system had no eyes. Games")
            print("  exist. Run:  python doctor.py  (it pinpoints + auto-fixes;")
            print("  usual cause: a stray comma in data/credentials.json, or")
            print("  keys saved in the Documents clone instead of Desktop).")
            if tg.is_configured:
                tg.send("SQUAD blocked: no API keys loaded. Run python doctor.py")
            return

    candidates = None
    for max_h in (6.0, 12.0, 24.0, 48.0):
        feed = OddsApiFeed(min_hours_ahead=0.25, max_hours_ahead=max_h)
        cands = feed.collect()
        if cands:
            candidates = cands
            break
        print(f"  .. nothing inside {max_h:.0f}h - widening ...")
    if not candidates:
        print()
        print("  [FAIL] feed reachable but zero candidates in 48h.")
        print("  Run:  python doctor.py  (checks clock skew + key health).")
        return

    comparator = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0)
    opps = [comparator.evaluate(s, q) for s, q in candidates]
    clean = [o for o in opps if not _is_suspect(o)]
    clean = _drop_degenerate(clean)
    clean = [o for o in clean
             if MIN_ODDS <= o.decimal_odds <= MAX_ODDS
             and o.selection.model_probability >= MIN_PROB
             and len(o.quotes_by_book) >= 2]

    pending = _pending_keys(logger)
    picks, used = [], set()
    for o in sorted(clean,
                    key=lambda x: (x.selection.model_probability, x.edge),
                    reverse=True):
        if len(picks) >= len(platforms) * LEGS_PER_SLIP:
            break
        k = (o.selection.match_id, o.selection.market, o.selection.selection)
        if o.selection.match_id in used or k in pending:
            continue
        picks.append(o)
        used.add(o.selection.match_id)

    if not picks:
        print("\n  Every candidate is already PENDING in the ledger.")
        print("  Force fresh prices:  Remove-Item data/feed_cache.json")
        return

    kmap = _kickoff_map()
    picks.sort(key=lambda o: kmap.get(o.selection.match_id, "9999-12-31"))

    print(f"\n  Pool: {len(picks)} picks, soonest kickoff first "
          f"([V]=measured edge):")
    for i, o in enumerate(picks, start=1):
        mark = "[V]" if o.edge >= 0.02 else "   "
        print(f"   {i:>2}. {mark} {_trunc(o.selection.match_label, 44):<44} "
              f"{o.selection.market}->{o.selection.selection:<6} "
              f"@ {o.decimal_odds:<5.2f} p={o.selection.model_probability:.0%}")

    bankroll = Bankroll()
    placed = []
    for i, platform in enumerate(platforms):
        legs = picks[i::len(platforms)][:LEGS_PER_SLIP]
        if not legs:
            continue
        slip = Slip(slip_type="SQUAD",
                    legs=[SlipLeg.from_opportunity(l) for l in legs],
                    stake_units=stake)
        if not bankroll.can_place(slip.stake_units):
            print(f"  !! exposure cap blocked the {platform} slip - "
                  f"settle pending bets or lower --stake.")
            continue
        bankroll.register_bet(slip.stake_units)
        bet_id = logger.log_slip(slip, session=session.name)
        placed.append((platform, slip, bet_id))
        sheet = choose_platform(slip)
        text = (dc_replace(sheet, platform=platform).render(bet_id=bet_id)
                if sheet is not None else slip.render())
        print()
        print(text)
        print(f"  -> logged as {bet_id} (PENDING, {session.name}, {platform})")
        if tg.is_configured:
            tg.send(f"SQUAD {i + 1}/{len(platforms)} -> {platform.upper()}\n\n{text}")

    if placed and tg.is_configured:
        tot = sum(s.stake_units for _, s, _ in placed)
        exp = sum(s.stake_units * s.ev_per_unit for _, s, _ in placed)
        tg.send(f"SQUAD summary: {len(placed)} slips on {len(platforms)} "
                f"platforms, {tot:.2f}u staked, expected P/L {exp:+.2f}u. "
                f"One pick per platform - never duplicated.")

    print("\n  RULE: each pick lives on ONE platform only.")
    print("  Settle as games finish:  python settle.py")


if __name__ == "__main__":
    main()