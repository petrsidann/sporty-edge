"""
smart_picks.py (DUAL-LANE FINAL)
Every in-window match can yield up to 2 picks:
  SIDE   : clear favorite -> pick it; tight match -> higher-prob side
  TOTALS : the best Over/Under line (your draw-insight: goals ignore the coin flip)
8 picks/session across 5 platforms (BetPawa, Betika, LuckyPari, WekaWin,
BetJam), rolling 4h window, every pick settles inside it. Win probability
sets the stake. Same-match pairs are flagged as correlated.
Engines: soccer strength blend (history.csv) + log5 ratings
(tennis/NBA/NFL/MLB) blended 0.4 model / 0.6 consensus, tagged [MODEL].
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from utils.term import force_utf8_stdio
    force_utf8_stdio()
except Exception:
    pass

from feeds.oddsapi import OddsApiFeed
from notify.telegram import TelegramNotifier
from odds.comparator import BetOpportunity, OddsComparator
from slips.generator import Slip, SlipLeg
from utils.bankroll import Bankroll
from utils.logger import BetLogger
from utils.session import EAT, clock_line, detect

PLATFORMS: list[str] = ["BetPawa", "Betika", "LuckyPari", "WekaWin", "BetJam"]
N_PICKS = 8
WINDOW_HOURS = 4.0
CACHE = Path("data") / "feed_cache.json"

DURATIONS = [
    ("mlb", 2.9), ("kbo", 2.9), ("npb", 2.9), ("baseball", 2.9),
    ("nfl", 3.2), ("ncaaf", 3.3), ("nba", 2.4), ("basketball", 2.4),
    ("nhl", 2.6), ("hockey", 2.6), ("mma", 1.5), ("tennis", 2.2),
]

try:
    from models.ratings import sport_of_league, pair_prob
except Exception:
    sport_of_league = None
    pair_prob = None

try:
    from models.probability_engine import ExpectedGoals, PoissonMatchModel
    from models import strength_engine
except Exception:
    strength_engine = None


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
        return f"Double chance {selection} (confirm price on app)"
    return f"{market} -> {selection}"


def _pending_families(logger: BetLogger) -> dict[str, set[str]]:
    """match_id -> {'side','ou'} families already pending for that match."""
    fams: dict[str, set[str]] = defaultdict(set)
    for rec in logger.pending():
        for leg in rec.get("legs", []):
            m = str(leg.get("market", "")).upper()
            fam = "side" if m in ("1X2", "ML", "MONEYLINE") or m.startswith("DC") else "ou"
            fams[str(leg.get("match_id"))].add(fam)
    return fams


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
    if prob >= 0.80:
        return "HIT", 1.00
    if prob >= 0.65:
        return "WARM", 0.50
    if prob >= 0.55:
        return "STEADY", 0.35
    return "SPICY", 0.15


def _blend(league: str, home: str, away: str,
           p_home: float, p_away: float) -> tuple[float, float] | None:
    """0.4 model + 0.6 consensus. Soccer via strength engine; other sports
    via log5 win-rate ratings. Returns adjusted (p_home, p_away) or None."""
    try:
        if strength_engine is not None:
            xg = strength_engine.expected_goals(home, away, league)
            m = PoissonMatchModel(xg)
            r = m.one_x_two()
            return (0.4 * r["Home"] + 0.6 * p_home,
                    0.4 * r["Away"] + 0.6 * p_away)
    except Exception:
        pass
    try:
        if sport_of_league is not None and pair_prob is not None:
            sport = sport_of_league(league)
            if sport is not None:
                pm = pair_prob(sport, home, away)
                if pm is not None:
                    return (0.4 * pm + 0.6 * p_home,
                            0.4 * (1.0 - pm) + 0.6 * p_away)
    except Exception:
        pass
    return None


def _rebuild(opp: BetOpportunity, new_prob: float) -> BetOpportunity:
    sel = dc_replace(opp.selection, model_probability=new_prob)
    return dc_replace(opp, selection=sel)


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
    print(f"  SMART PICKS DUAL-LANE | {session.emoji} {session.name} | "
          f"window NOW -> +{WINDOW_HOURS:.0f}h")
    print(f"  {clock_line()}")
    print("=" * 70)

    cands = OddsApiFeed(min_hours_ahead=0.0, max_hours_ahead=WINDOW_HOURS,
                        markets="h2h,totals").collect()
    if not cands:
        age_h = 99.0
        try:
            age_h = (time.time() - CACHE.stat().st_mtime) / 3600.0
        except OSError:
            pass
        if age_h < 2.0:
            msg = (f"{session.emoji} {session.name}: no games start in this "
                   f"4h window (global dead hours). Loop keeps watching.")
        else:
            msg = "[FAIL] feed fetch failed - run: python doctor.py"
        print(msg)
        if tg.is_configured:
            tg.send(msg)
        return

    comparator = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0)
    fams = _pending_families(logger)
    kmap = _kickoff_map()

    h2h: dict[str, dict[str, BetOpportunity]] = defaultdict(dict)
    totals: dict[str, list[BetOpportunity]] = defaultdict(list)

    for sel, quotes in cands:
        opp = comparator.evaluate(sel, quotes)
        prob = opp.selection.model_probability
        if not 0.05 <= prob <= 0.95:
            continue
        ko = _kickoff_dt(kmap.get(opp.selection.match_id, ""))
        if ko is None or ko < now:
            continue
        if (ko + timedelta(hours=sport_duration(opp.selection.league))) > window_end:
            continue
        m = opp.selection.market.upper()
        if m in ("1X2", "ML", "MONEYLINE"):
            h2h[opp.selection.match_id][opp.selection.selection] = opp
        elif m.startswith("O/U"):
            totals[opp.selection.match_id].append(opp)

    candidates: list[tuple[BetOpportunity, str]] = []
    model_tagged: set[str] = set()

    for mid, sides in h2h.items():
        draw = sides.get("Draw")
        home, away = sides.get("Home"), sides.get("Away")
        if home is None or away is None:
            continue
        p_home, p_away = home.selection.model_probability, away.selection.model_probability
        league = home.selection.league
        hn, an = _teams(home.selection.match_label)
        blended = _blend(league, hn, an, p_home, p_away)
        if blended is not None:
            p_home, p_away = blended
            home, away = _rebuild(home, p_home), _rebuild(away, p_away)
            model_tagged.add(mid)
        best = home if p_home >= p_away else away
        best_p = max(p_home, p_away)
        draw_p = draw.selection.model_probability if draw else 0.0
        tight = (draw_p >= 0.28) or (best_p < 0.50)

        # SIDE lane
        fams_mid = fams.get(mid, set())
        if "side" not in fams_mid:
            lane = "WINNER" if best_p >= 0.50 and not tight else "SIDE"
            candidates.append((best, lane))
        # TOTALS lane (always, if a line exists)
        if "ou" not in fams_mid:
            lines = totals.get(mid, [])
            if lines:
                top = max(lines, key=lambda o: o.selection.model_probability)
                candidates.append((top, "TOTALS"))

    for mid, lines in totals.items():
        if mid in h2h or not lines:
            continue
        if "ou" in fams.get(mid, set()):
            continue
        top = max(lines, key=lambda o: o.selection.model_probability)
        candidates.append((top, "TOTALS"))

    seen: set = set()
    fresh: list[tuple[BetOpportunity, str]] = []
    for o, lane in candidates:
        k = (o.selection.match_id, o.selection.market, o.selection.selection)
        if k in seen:
            continue
        seen.add(k)
        fresh.append((o, lane))

    fresh.sort(key=lambda t: t[0].selection.model_probability, reverse=True)
    picks = fresh[:N_PICKS]

    print(f"\n  {len(fresh)} candidate picks in-window -> taking {len(picks)}\n")

    if not picks:
        msg = (f"{session.emoji} {session.name}: every qualifying game is "
               f"already logged. Next cycle keeps watching for new listings.")
        print(msg)
        if tg.is_configured:
            tg.send(msg)
        return

    bankroll = Bankroll()
    placed: list[tuple[str, BetOpportunity, str, str, float]] = []
    match_slot: dict[str, int] = {}

    for i, (opp, lane) in enumerate(picks, start=1):
        platform = PLATFORMS[(i - 1) % len(PLATFORMS)]
        prob = opp.selection.model_probability
        label, mult = _tier(prob)
        eff = round(base_stake * mult, 2)
        slip = Slip(slip_type="SINGLE",
                    legs=[SlipLeg.from_opportunity(opp)],
                    stake_units=eff)
        if not bankroll.can_place(eff):
            print(f"  !! exposure cap blocked {platform} - settle pending first.")
            continue
        bankroll.register_bet(eff)
        bet_id = logger.log_slip(slip, session=session.name)
        placed.append((platform, opp, bet_id, label, eff))
        match_slot.setdefault(opp.selection.match_id, i)

        leg = slip.legs[0]
        floor = round(leg.decimal_odds * 0.97, 2)
        ko = _kickoff_dt(kmap.get(leg.match_id, ""))
        done = ((ko + timedelta(hours=sport_duration(opp.selection.league)))
                .astimezone(EAT).strftime("%H:%M EAT") if ko else "?")
        pair_note = ""
        if opp.selection.match_id in match_slot and match_slot[opp.selection.match_id] < i:
            pair_note = (f"\n(PAIR with pick #{match_slot[opp.selection.match_id]} "
                         f"same match - correlated: often win/lose together)")
        tag = " [MODEL]" if opp.selection.match_id in model_tagged else ""
        msg = (f"{lane}{tag} {i}/{len(picks)} -> {platform.upper()} [{label}]\n"
               f"{leg.match_label.split(' · ')[0]}\n"
               f"KICKOFF {_kickoff_eat(kmap.get(leg.match_id, ''))} | "
               f"settles ~{done} (inside window)\n"
               f"PICK: {_anchor(leg.market, leg.selection, leg.match_label)}\n"
               f"Win prob {prob:.0%} | take {leg.decimal_odds:.2f} | "
               f"place if app >= {floor}"
               f"{pair_note}\n"
               f"Stake {eff:.2f}u | {bet_id}")
        print(msg + "\n")
        if tg.is_configured:
            tg.send(msg)

    if placed:
        exp = sum(o.ev_per_unit * e for _, o, _, _, e in placed)
        total = sum(e for _, _, _, _, e in placed)
        summary = (f"{len(placed)}/{N_PICKS} picks | stake {total:.2f}u | "
                   f"expected {exp:+.2f}u | all settle by "
                   f"{window_end.astimezone(EAT).strftime('%H:%M EAT')}")
        print(summary)
        if tg.is_configured:
            tg.send(summary)


if __name__ == "__main__":
    main()