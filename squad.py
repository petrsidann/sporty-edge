"""
squad.py v5 - 4 platform slips. Fixes v4's two bugs:
  1. Sheets are SELF-CONTAINED: legs always rendered from the slip itself
     (v4 passed an empty list - sheets printed with no picks).
  2. Lottery-proof grading: combined odds capped at 8.0, combined win
     probability floored at 10%.  EDGE+ requires EV>=+2% AND prob>=12%.
     NEUTRAL = half stake.  FUN = 0.25u cap.  4.4%-prob tickets can no
     longer be labeled EDGE+.

    python squad.py               # 4 slips (SportyBet/Betika/BetPawa/1xBet)
    python squad.py --stake 0.25
    python squad.py --slips 3
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.settings import ACTION_SETTINGS, PLACEABLE_BOOKS
from feeds.oddsapi import OddsApiFeed
from notify.telegram import TelegramNotifier
from odds.comparator import BetOpportunity, OddsComparator
from slips.generator import Slip, SlipLeg
from utils.bankroll import Bankroll
from utils.logger import BetLogger
from utils.session import EAT, SESSIONS, clock_line, detect

SUSPECT_RATIO = 1.20
LEGS_PER_SLIP = 4
ODDS_CAP = 8.0          # combined odds ceiling - no lottery tickets
PROB_FLOOR = 0.10       # combined win-probability floor
MIN_ODDS = ACTION_SETTINGS.min_odds
MAX_ODDS = ACTION_SETTINGS.max_odds
MIN_PROB = ACTION_SETTINGS.min_prob
GAME_HOURS = 2.5
_CACHE = Path("data") / "feed_cache.json"


def _trunc(t: str, w: int) -> str:
    return t if len(t) <= w else t[: w - 1] + "..."


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
    if m.startswith("SPREAD"):
        team = home if selection.endswith("Home") else away
        return f"{team} +spread (verify handicap sign on app)"
    if m.startswith("O/U"):
        return f"{market.replace('O/U ', '')} {selection} (match total)"
    if m.startswith("BTTS"):
        return f"Both teams to score: {selection}"
    if m.startswith("DC"):
        return f"Double chance {selection}"
    return f"{market} -> {selection}"


def _session_bound_hours() -> float:
    now = datetime.now(timezone.utc)
    now_min = now.hour * 60 + now.minute
    starts = sorted(s.utc_hour * 60 + s.utc_minute for s in SESSIONS)
    nxt = next((s for s in starts if s > now_min), starts[0] + 1440)
    return max(0.5, min((nxt - now_min) / 60.0 - GAME_HOURS, 12.0))


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
            return float(argv[i + 1]) if default.__class__ is float else int(argv[i + 1])
        except (ValueError, IndexError):
            return default
    return default


def _grade(ev: float, prob: float) -> tuple[str, float]:
    if ev >= 0.02 and prob >= 0.12:
        return "EDGE+", 1.00
    if ev >= -0.03:
        return "NEUTRAL", 0.50
    return "FUN", 0.50


def _render_sheet(slip, platform: str, bet_id: str, kmap: dict) -> str:
    """SELF-CONTAINED sheet: legs always come from the slip itself."""
    w = 78
    bar, sub = "=" * w, "-" * w
    out = [bar,
           f" PICK SHEET {bet_id} | {slip.slip_type} ({slip.n_legs} picks)",
           f" LOAD ON >>> {platform.upper()}",
           " CLICK THE TEAM NAME - never a Home/Away button.",
           sub]
    for i, leg in enumerate(slip.legs, start=1):
        out.append(f" {i}. MATCH  : {leg.match_label.split(' · ')[0]}")
        out.append(f"    KICKOFF: {_kickoff_eat(kmap.get(leg.match_id, ''))}")
        out.append(f"    PICK   : {_anchor(leg.market, leg.selection, leg.match_label)}")
        out.append(f"    PRICE  : take {leg.decimal_odds:.2f} | place only if app >= "
                   f"{round(leg.decimal_odds * 0.97, 2):.2f}")
    out += [sub,
            f" Combined odds : {slip.combined_odds:.2f}",
            f" TRUE win prob : {slip.combined_prob * 100:.1f}% "
            f"(~{slip.combined_prob * 10:.0f} of 10)",
            f" Slip EV       : {slip.ev_per_unit * 100:+.1f}%  |  "
            f"Stake {slip.stake_units:.2f}u",
            sub,
            " Settle automatically after the games:  python settle.py"]
    return "\n".join(out)


def main() -> None:
    stake = _flag(sys.argv, "--stake", 0.5)
    n_slips = int(_flag(sys.argv, "--slips", 4))
    n_slips = max(1, min(n_slips, len(PLACEABLE_BOOKS)))
    platforms = list(PLACEABLE_BOOKS[:n_slips])
    session = detect()
    tg = TelegramNotifier()
    logger = BetLogger()

    bound = _session_bound_hours()
    print("=" * 66)
    print(f"  SQUAD v5 | {session.emoji} {session.name} | {len(platforms)} slips "
          f"@ {stake}u base | odds cap {ODDS_CAP:g}")
    print(f"  {clock_line()}")
    print("=" * 66)

    feed = OddsApiFeed(min_hours_ahead=0.25, max_hours_ahead=bound)
    if not feed.is_configured:
        print("\n  [FAIL] NO API KEYS LOADED. Run:  python doctor.py")
        return

    candidates = None
    for max_h in (bound, 12.0, 24.0, 48.0):
        cands = OddsApiFeed(min_hours_ahead=0.25, max_hours_ahead=max_h).collect()
        if cands:
            candidates = cands
            break
        print(f"  .. nothing inside {max_h:.0f}h - widening ...")
    if not candidates:
        print("\n  [FAIL] no candidates even at 48h. Run:  python doctor.py")
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
        print("\n  All candidates already PENDING. Fresh prices: "
              "Remove-Item data/feed_cache.json")
        return

    kmap = _kickoff_map()
    picks.sort(key=lambda o: kmap.get(o.selection.match_id, "9999-12-31"))

    print(f"\n  Pool: {len(picks)} picks by kickoff (EV per leg shown):")
    for i, o in enumerate(picks, start=1):
        print(f"   {i:>2}. {_trunc(o.selection.match_label, 38):<38} "
              f"{_anchor(o.selection.market, o.selection.selection, o.selection.match_label)[:30]:<30} "
              f"@ {o.decimal_odds:<5.2f} EV {o.ev_per_unit * 100:+.1f}%")

    bankroll = Bankroll()
    placed = []
    for i, platform in enumerate(platforms):
        assigned = picks[i::len(platforms)]
        legs, used_matches = [], set()
        odds_prod, prob_prod = 1.0, 1.0
        for o in assigned:  # edge-ranked order; cap enforces sane tickets
            if len(legs) >= LEGS_PER_SLIP:
                break
            if o.selection.match_id in used_matches:
                continue
            new_odds = odds_prod * o.decimal_odds
            new_prob = prob_prod * o.selection.model_probability
            if new_odds > ODDS_CAP or new_prob < PROB_FLOOR:
                continue
            legs.append(o)
            used_matches.add(o.selection.match_id)
            odds_prod, prob_prod = new_odds, new_prob
        if not legs:
            print(f"  .. {platform}: no legs fit inside the odds cap - skipped.")
            continue
        legs.sort(key=lambda o: kmap.get(o.selection.match_id, "9999-12-31"))
        proto = Slip(slip_type="SQUAD",
                     legs=[SlipLeg.from_opportunity(l) for l in legs],
                     stake_units=stake)
        label, mult = _grade(proto.ev_per_unit, proto.combined_prob)
        eff = stake * mult if label != "FUN" else min(stake, 0.25)
        slip = dc_replace(proto, stake_units=eff)
        if not bankroll.can_place(slip.stake_units):
            print(f"  !! exposure cap blocked the {platform} slip.")
            continue
        bankroll.register_bet(slip.stake_units)
        bet_id = logger.log_slip(slip, session=session.name)
        placed.append((platform, slip, bet_id, label))
        text = _render_sheet(slip, platform, bet_id, kmap)
        print()
        print(f"  [{label}] stake {eff:.2f}u ({len(legs)} legs)")
        print(text)
        if tg.is_configured:
            tg.send(f"SQUAD {i + 1}/{len(platforms)} -> {platform.upper()} "
                    f"[{label}]\n\n{text}")

    if placed and tg.is_configured:
        tot = sum(s.stake_units for _, s, _, _ in placed)
        exp = sum(s.stake_units * s.ev_per_unit for _, s, _, _ in placed)
        tg.send(f"SQUAD: {len(placed)} slips, {tot:.2f}u, expected {exp:+.2f}u. "
                f"Click TEAM NAMES. Auto-settle: python settle.py")
    n_edge = sum(1 for _, _, _, l in placed if l == "EDGE+")
    print("\n  VERDICT: "
          + (f"{n_edge}/{len(placed)} slips EDGE+ (full stakes justified)."
             if n_edge else
             "0 EDGE+ slips this session - market priced efficiently; "
             "NEUTRAL/FUN stakes only."))
    print("  Settle:  python settle.py")


if __name__ == "__main__":
    main()