"""
squad.py v3 - 4 platform slips x 4 legs, disjoint matches, kickoff-ordered,
legs chosen by EDGE (EV) FIRST, probability second. Never stands down while
games exist - but every slip is graded honestly and staked accordingly:

    EDGE+    combined EV >= +2%    -> stake as planned
    NEUTRAL  combined EV >= -3%    -> half stake
    FUN      below that            -> 0.25u max (expected loss - entertainment)

    python squad.py                # 4 slips (SportyBet/Betika/BetPawa/1xBet)
    python squad.py --stake 0.25
    python squad.py --slips 3
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


def _grade(ev: float) -> tuple[str, float, str]:
    """(label, stake multiplier, advice) from a slip's combined EV."""
    if ev >= 0.02:
        return "EDGE+", 1.00, "value confirmed - stake as planned"
    if ev >= -0.03:
        return "NEUTRAL", 0.50, "roughly break-even - half stake"
    return "FUN", 0.50 if False else 0.0, "expected loss - 0.25u max or skip"


def main() -> None:
    stake = _flag(sys.argv, "--stake", 0.5)
    n_slips = int(_flag(sys.argv, "--slips", 4))
    n_slips = max(1, min(n_slips, len(PLACEABLE_BOOKS)))
    platforms = list(PLACEABLE_BOOKS[:n_slips])
    session = detect()
    tg = TelegramNotifier()
    logger = BetLogger()

    print("=" * 66)
    print(f"  SQUAD v3 | {session.emoji} {session.name} | {len(platforms)} slips x "
          f"{LEGS_PER_SLIP} legs @ {stake}u base")
    print(f"  {clock_line()}")
    print(f"  Load targets: {', '.join(p.upper() for p in platforms)}")
    print("=" * 66)

    if "--refresh" not in sys.argv:
        feed = OddsApiFeed(min_hours_ahead=0.25, max_hours_ahead=6.0)
        if not feed.is_configured:
            print("\n  [FAIL] NO API KEYS LOADED. Run:  python doctor.py")
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
        print("\n  [FAIL] feed reachable but zero candidates in 48h. Run doctor.")
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
    # EDGE-FIRST selection: best EV legs, then most likely, disjoint matches.
    picks, used = [], set()
    for o in sorted(clean,
                    key=lambda x: (x.ev_per_unit, x.selection.model_probability),
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

    n_value = sum(1 for o in picks if o.edge >= 0.02)
    print(f"\n  Pool: {len(picks)} picks by kickoff "
          f"({n_value} with measured edge [V]):")
    for i, o in enumerate(picks, start=1):
        mark = "[V]" if o.edge >= 0.02 else "   "
        print(f"   {i:>2}. {mark} {_trunc(o.selection.match_label, 42):<42} "
              f"{o.selection.market}->{o.selection.selection:<6} "
              f"@ {o.decimal_odds:<5.2f} p={o.selection.model_probability:.0%} "
              f"EV {o.ev_per_unit * 100:+.1f}%")

    bankroll = Bankroll()
    placed = []
    for i, platform in enumerate(platforms):
        legs = picks[i::len(platforms)][:LEGS_PER_SLIP]
        if not legs:
            continue
        proto = Slip(slip_type="SQUAD",
                     legs=[SlipLeg.from_opportunity(l) for l in legs],
                     stake_units=stake)
        label, mult, advice = _grade(proto.ev_per_unit)
        eff_stake = stake * mult if label == "NEUTRAL" else (
            min(stake, 0.25) if label == "FUN" else stake)
        slip = dc_replace(proto, stake_units=eff_stake)

        if not bankroll.can_place(slip.stake_units):
            print(f"  !! exposure cap blocked the {platform} slip.")
            continue
        bankroll.register_bet(slip.stake_units)
        bet_id = logger.log_slip(slip, session=session.name)
        placed.append((platform, slip, bet_id, label))
        sheet = choose_platform(slip)
        text = (dc_replace(sheet, platform=platform).render(bet_id=bet_id)
                if sheet is not None else slip.render())
        print()
        print(f"  [{label}] {advice}  (stake adjusted to {eff_stake:.2f}u)")
        print(text)
        print(f"  -> logged as {bet_id} (PENDING, {session.name}, {platform})")
        if tg.is_configured:
            tg.send(f"SQUAD {i + 1}/{len(platforms)} -> {platform.upper()} "
                    f"[{label}]\n\n{text}")

    if placed and tg.is_configured:
        tot = sum(s.stake_units for _, s, _, _ in placed)
        exp = sum(s.stake_units * s.ev_per_unit for _, s, _, _ in placed)
        grades = ", ".join(f"{p[:2].upper()}:{l}" for p, _, _, l in placed)
        tg.send(f"SQUAD summary: {len(placed)} slips [{grades}], "
                f"{tot:.2f}u staked, expected P/L {exp:+.2f}u. "
                f"EDGE+ = stake as planned | NEUTRAL = half | FUN = tiny/skip.")

    n_edge = sum(1 for _, _, _, l in placed if l == "EDGE+")
    print("\n  VERDICT: "
          + (f"{n_edge}/{len(placed)} slips are EDGE+ (worth full stakes)."
             if n_edge else
             "0 EDGE+ slips right now - FUN/NEUTRAL stakes only, or wait for "
             "the next session; the market is pricing efficiently.")
    )
    print("  Settle as games finish:  python settle.py")


if __name__ == "__main__":
    main()