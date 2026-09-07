"""
smart_picks.py (ALWAYS-ON BOARD) - never-empty scanner.

Zones (every pick labeled, timeline always visible):
    [LIVE]     settles inside 4h        (strict session rule)
    [TODAY]    settles in 4-12h         (tonight's board)
    [TMRW]     settles in 12-30h        (next-day board)

Markets per match (up to 2 picks, dedupe-safe):
    SIDE   : best side of 1X2/ML (the favorite, or higher-prob side when tight)
    DC     : derived Double Chance (p_home+p_draw or p_away+p_draw, 5%
             haircut on price - CONFIRM the DC price on your app)
    TOTALS : best Over/Under line (goals ignore the coin flip)

Selection: zone priority LIVE->TODAY->TMRW, then highest win probability.
Emits min 10, up to 25 when supply is fat. 5 platforms round-robin.
Win probability sets the stake: HIT>=80% 1.0x | WARM>=65% 0.5x |
STEADY>=55% 0.35x | SPICY 0.15x.  Only excluded: already-pending
picks/matches and broken feed lines (>2.5x off consensus).
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
from odds.comparator import BetOpportunity, OddsComparator, Selection
from slips.generator import Slip, SlipLeg
from utils.bankroll import Bankroll
from utils.logger import BetLogger
from utils.session import EAT, clock_line, detect

PLATFORMS: list[str] = ["BetPawa", "Betika", "LuckyPari", "WekaWin", "BetJam"]
N_MIN = 10
N_MAX = 25
FETCH_HOURS = 30
CACHE = Path("data") / "feed_cache.json"
DC_HAIRCUT = 0.95

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
    if m == "DC":
        side = "Home or Draw" if selection == "1X" else "Draw or Away"
        return f"Double Chance {selection} ({side}) - CONFIRM price on app"
    if m.startswith("O/U"):
        return f"{selection} {market.replace('O/U ', '')} goals"
    return f"{market} -> {selection}"


def _pending(logger: BetLogger) -> tuple[set, set]:
    keys: set = set()
    matches: set = set()
    for rec in logger.pending():
        for leg in rec.get("legs", []):
            keys.add((leg.get("match_id"), leg.get("market"),
                      leg.get("selection")))
            matches.add(leg.get("match_id"))
    return keys, matches


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
    try:
        if strength_engine is not None:
            xg = strength_engine.expected_goals(home, away, league)
            r = PoissonMatchModel(xg).one_x_two()
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


def _zone(ko: datetime, dur: float, now: datetime) -> str | None:
    settle = ko + timedelta(hours=dur)
    if settle <= now + timedelta(hours=4):
        return "LIVE"
    if settle <= now + timedelta(hours=12):
        return "TODAY"
    if settle <= now + timedelta(hours=30):
        return "TMRW"
    return None


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

    print("=" * 70)
    print(f"  SMART PICKS ALWAYS-ON | {session.emoji} {session.name}")
    print(f"  {clock_line()}")
    print("=" * 70)

    cands = OddsApiFeed(min_hours_ahead=0.0, max_hours_ahead=FETCH_HOURS,
                        markets="h2h,totals").collect()
    if not cands:
        msg = "[FAIL] feed returned nothing - run: python doctor.py"
        print(msg)
        if tg.is_configured:
            tg.send(msg)
        return

    comparator = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0)
    pending_keys, pending_matches = _pending(logger)
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
        if _zone(ko, sport_duration(opp.selection.league), now) is None:
            continue
        m = opp.selection.market.upper()
        if m in ("1X2", "ML", "MONEYLINE"):
            h2h[opp.selection.match_id][opp.selection.selection] = opp
        elif m.startswith("O/U"):
            totals[opp.selection.match_id].append(opp)

    candidates: list[tuple[BetOpportunity, str, str]] = []
    model_tagged: set[str] = set()

    for mid, sides in h2h.items():
        home, away = sides.get("Home"), sides.get("Away")
        draw = sides.get("Draw")
        if home is None or away is None:
            continue
        p_home, p_away = (home.selection.model_probability,
                          away.selection.model_probability)
        league = home.selection.league
        hn, an = _teams(home.selection.match_label)
        bl = _blend(league, hn, an, p_home, p_away)
        if bl is not None:
            p_home, p_away = bl
            home, away = (dc_replace(home, selection=dc_replace(
                home.selection, model_probability=p_home)),
                dc_replace(away, selection=dc_replace(
                    away.selection, model_probability=p_away)))
            model_tagged.add(mid)
        if draw is not None:
            draw = dc_replace(draw, selection=dc_replace(
                draw.selection, model_probability=max(
                    0.02, 1.0 - p_home - p_away)))
        ko = _kickoff_dt(kmap.get(mid, ""))
        if ko is None:
            continue
        zone = _zone(ko, sport_duration(league), now)
        if zone is None:
            continue

        fams_count = 0
        best = home if p_home >= p_away else away
        best_p = max(p_home, p_away)
        k_side = (mid, best.selection.market, best.selection.selection)
        if k_side not in pending_keys and mid not in pending_matches:
            candidates.append((best, "SIDE", zone))
            fams_count += 1

        # DC derived from de-vigged consensus (higher-prob coverage side)
        if draw is not None:
            p_d = draw.selection.model_probability
            if p_home >= p_away:
                p_dc, dc_sel = p_home + p_d, "1X"
            else:
                p_dc, dc_sel = p_away + p_d, "X2"
            p_dc = min(p_dc, 0.96)
            if p_dc >= 0.55:
                taken = round(1.0 / p_dc * DC_HAIRCUT, 2)
                if taken >= 1.05:
                    sel = Selection(match_id=mid, match_label=home.selection.match_label,
                                    league=league, market="DC", selection=dc_sel,
                                    model_probability=p_dc)
                    opp_dc = comparator.evaluate(sel, [type(home).__mro__ and __import__(
                        "odds.comparator", fromlist=["OddsQuote"]).OddsQuote(
                        book="derived", decimal_odds=taken)])
                    k_dc = (mid, "DC", dc_sel)
                    if k_dc not in pending_keys and fams_count < 2:
                        candidates.append((opp_dc, "DC", zone))
                        fams_count += 1

        lines = totals.get(mid, [])
        if lines:
            top = max(lines, key=lambda o: o.selection.model_probability)
            k_ou = (mid, top.selection.market, top.selection.selection)
            if k_ou not in pending_keys and fams_count < 2:
                candidates.append((top, "TOTALS", zone))
                fams_count += 1

    for mid, lines in totals.items():
        if mid in h2h or not lines:
            continue
        if "ou" in set():
            continue
        top = max(lines, key=lambda o: o.selection.model_probability)
        k_ou = (mid, top.selection.market, top.selection.selection)
        if k_ou not in pending_keys and mid not in pending_matches:
            ko = _kickoff_dt(kmap.get(mid, ""))
            if ko is None:
                continue
            zone = _zone(ko, sport_duration(lines[0].selection.league), now)
            if zone:
                candidates.append((top, "TOTALS", zone))

    seen: set = set()
    fresh: list[tuple[BetOpportunity, str, str]] = []
    for o, lane, zone in candidates:
        k = (o.selection.match_id, o.selection.market, o.selection.selection)
        if k in seen:
            continue
        seen.add(k)
        fresh.append((o, lane, zone))

    zone_rank = {"LIVE": 0, "TODAY": 1, "TMRW": 2}
    fresh.sort(key=lambda t: (zone_rank.get(t[2], 9),
                              -t[0].selection.model_probability))
    picks = fresh[:N_MAX]

    counts = defaultdict(int)
    for _, _, z in picks:
        counts[z] += 1
    print(f"\n  Supply: {len(fresh)} candidates "
          f"({len(live_count(picks))} LIVE) -> emitting {len(picks)} "
          f"(zones: {dict(counts)})\n")

    if not picks:
        msg = "[FAIL] no events at all on the feed - run: python doctor.py"
        print(msg)
        if tg.is_configured:
            tg.send(msg)
        return

    bankroll = Bankroll()
    for i, (opp, lane, zone) in enumerate(picks, start=1):
        platform = PLATFORMS[(i - 1) % len(PLATFORMS)]
        prob = opp.selection.model_probability
        label, mult = _tier(prob)
        eff = round(base_stake * mult, 2)
        slip = Slip(slip_type="SINGLE",
                    legs=[SlipLeg.from_opportunity(opp)], stake_units=eff)
        if not bankroll.can_place(eff):
            print(f"  !! exposure cap reached at pick {i} - placed {i-1}.")
            break
        bankroll.register_bet(eff)
        bet_id = logger.log_slip(slip, session=session.name)
        leg = slip.legs[0]
        floor = round(leg.decimal_odds * 0.97, 2)
        ko = _kickoff_dt(kmap.get(leg.match_id, ""))
        done = ((ko + timedelta(hours=sport_duration(opp.selection.league)))
                .astimezone(EAT).strftime("%H:%M EAT") if ko else "?")
        tag = " [MODEL]" if opp.selection.match_id in model_tagged else ""
        msg = (f"{zone}{tag} {lane} {i}/{len(picks)} -> {platform.upper()} "
               f"[{label}]\n"
               f"{leg.match_label.split(' · ')[0]}\n"
               f"KICKOFF {_kickoff_eat(kmap.get(leg.match_id, ''))} | "
               f"settles ~{done}\n"
               f"PICK: {_anchor(leg.market, leg.selection, leg.match_label)}\n"
               f"Win prob {prob:.0%} | take {leg.decimal_odds:.2f} | "
               f"place if app >= {floor}\n"
               f"Stake {eff:.2f}u | {bet_id}")
        print(msg + "\n")
        if tg.is_configured:
            tg.send(msg)

    if tg.is_configured:
        tg.send(f"📦 {session.emoji} {session.name}: {len(picks)} picks this cycle "
                f"(LIVE first, then TODAY/TMRW). Next cycle tops up to {N_MAX}.")


def live_count(picks):
    return [p for p in picks if p[2] == "LIVE"]


if __name__ == "__main__":
    main()