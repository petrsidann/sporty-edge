"""smart_picks.py (OWNER-SPEC FINAL)
Per-session sport lists + per-session targets (10/10/25/10). All picks
settle before the session window ends. Lanes: HIT first (prob>=0.80),
then WINNER / TOTALS pivot on tight matches / DC derived. Hard cap =
session target minus already-logged-this-session (fixes over-fill).
Team-strength + log5 blending tagged [MODEL]. Telegram every pick.
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
from utils.session import EAT, current_or_next, clock_line

PLATFORMS: list[str] = ["BetPawa", "Betika", "LuckyPari", "WekaWin", "BetJam"]
CACHE = Path("data") / "feed_cache.json"
DC_HAIRCUT = 0.95
DISCOVER_TS = Path("data") / "sport_discovery.json"

# Per-session sport keys (invalid keys 422-skip free of charge)
SESSION_SPORTS: dict[str, tuple[str, ...]] = {
    "S1": ("baseball_mlb", "basketball_nba", "icehockey_nhl",
           "soccer_australia_aleague", "soccer_japan_j_league",
           "soccer_korea_kleague1", "basketball_ncaab"),
    "S2": ("soccer_russia_premier_league", "soccer_poland_ekstraklasa",
           "soccer_czech_first_div", "soccer_hungary_nb_i",
           "soccer_turkey_super_league", "soccer_greece_super_league"),
    "S3": ("soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a",
           "soccer_uefa_champs_league", "basketball_euroleague",
           "soccer_efl_champ", "soccer_germany_bundesliga",
           "soccer_france_ligue_one", "soccer_netherlands_eredivisie",
           "soccer_portugal_primeira_liga", "soccer_uefa_europa_league",
           "soccer_uefa_europa_conference_league"),
    "S4": ("soccer_brazil_campeonato", "soccer_brazil_serie_b",
           "soccer_argentina_primera_division", "soccer_mexico_ligamx",
           "soccer_usa_mls", "americanfootball_nfl", "basketball_nba",
           "americanfootball_ncaaf", "baseball_mlb", "icehockey_nhl"),
}

DURATIONS = [
    ("mlb", 2.9), ("kbo", 2.9), ("npb", 2.9), ("baseball", 2.9),
    ("nfl", 3.2), ("ncaaf", 3.3), ("ncaab", 2.7), ("nba", 2.4),
    ("basketball", 2.4), ("nhl", 2.6), ("hockey", 2.6),
    ("mma", 1.5), ("tennis", 2.2),
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
    for k, h in DURATIONS:
        if k in s:
            return h
    return 2.05


def _discover_tennis() -> tuple[str, ...]:
    """Active tennis tournament keys (1 credit, cached 6h)."""
    now = time.time()
    try:
        d = json.loads(DISCOVER_TS.read_text(encoding="utf-8-sig"))
        if now - float(d.get("ts", 0)) < 6 * 3600:
            return tuple(d.get("tennis", []))
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    try:
        from config.settings import FEED_SETTINGS
        import os
        key = (os.environ.get("ODDS_API_KEY", "").strip()
               or FEED_SETTINGS.odds_api_key)
        import urllib.request
        with urllib.request.urlopen(
                f"https://api.the-odds-api.com/v4/sports?apiKey={key}",
                timeout=15) as r:
            sports = json.loads(r.read().decode("utf-8"))
        tennis = tuple(s["key"] for s in sports
                       if s.get("key", "").startswith(("tennis_atp", "tennis_wta")))
        DISCOVER_TS.parent.mkdir(parents=True, exist_ok=True)
        DISCOVER_TS.write_text(json.dumps({"ts": now, "tennis": tennis}),
                               encoding="utf-8")
        return tennis
    except Exception:
        return ()


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


def _logged_this_session(logger: BetLogger, session_name: str,
                         window_start: datetime) -> int:
    ws_iso = window_start.isoformat(timespec="seconds")
    return sum(1 for rec in logger._read_all()
               if rec.get("session") == session_name
               and str(rec.get("logged_at", "")) >= ws_iso)


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


def _blend(league: str, home: str, away: str, p_home: float, p_away: float):
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


def main() -> None:
    base_stake = 0.5
    if "--stake" in sys.argv:
        i = sys.argv.index("--stake")
        try:
            base_stake = float(sys.argv[i + 1])
        except (ValueError, IndexError):
            pass

    session, win_start, win_end = current_or_next()
    tg = TelegramNotifier()
    logger = BetLogger()
    now = datetime.now(timezone.utc)
    hours = max(0.5, (win_end - now).total_seconds() / 3600.0)

    print("=" * 70)
    print(f"  {session.emoji} {session.name} | target {session.target} | "
          f"window ends {win_end.astimezone(EAT).strftime('%H:%M EAT')}")
    print(f"  {clock_line()}")
    print("=" * 70)

    already = _logged_this_session(logger, session.name, win_start)
    remaining = session.target - already
    if remaining <= 0:
        msg = (f"✅ {session.emoji} {session.name} fully covered - "
               f"{already}/{session.target} picks live. Money settles before "
               f"the next session.")
        print(msg)
        if tg.is_configured:
            tg.send(msg)
        return

    sports = list(SESSION_SPORTS.get(session.name, ()))
    if session.name in ("S2", "S3"):
        sports.extend(_discover_tennis())

    cands = OddsApiFeed(min_hours_ahead=0.0, max_hours_ahead=hours,
                        sports=tuple(sports),
                        markets="h2h,totals").collect()
    if not cands:
        msg = (f"{session.emoji} {session.name}: feed returned nothing this "
               f"cycle - next 30-min cycle rescans (already {already}/{session.target}).")
        print(msg)
        if tg.is_configured:
            tg.send(msg)
        return

    comparator = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0)
    kmap = _kickoff_map()

    pend_keys: set = set()
    pend_matches: set = set()
    for rec in logger.pending():
        for leg in rec.get("legs", []):
            pend_keys.add((leg.get("match_id"), leg.get("market"),
                           leg.get("selection")))
            pend_matches.add(leg.get("match_id"))

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
        if (ko + timedelta(hours=sport_duration(opp.selection.league))) > win_end:
            continue  # must settle inside this session
        m = opp.selection.market.upper()
        if m in ("1X2", "ML", "MONEYLINE"):
            h2h[opp.selection.match_id][opp.selection.selection] = opp
        elif m.startswith("O/U"):
            totals[opp.selection.match_id].append(opp)

    candidates: list[tuple[BetOpportunity, str]] = []
    model_tagged: set[str] = set()
    for mid, sides in h2h.items():
        home, away, draw = sides.get("Home"), sides.get("Away"), sides.get("Draw")
        if home is None or away is None:
            continue
        p_home, p_away = (home.selection.model_probability,
                          away.selection.model_probability)
        league = home.selection.league
        hn, an = _teams(home.selection.match_label)
        bl = _blend(league, hn, an, p_home, p_away)
        if bl is not None:
            p_home, p_away = bl
            home = dc_replace(home, selection=dc_replace(
                home.selection, model_probability=p_home))
            away = dc_replace(away, selection=dc_replace(
                away.selection, model_probability=p_away))
            model_tagged.add(mid)
        best = home if p_home >= p_away else away
        best_p = max(p_home, p_away)
        draw_p = draw.selection.model_probability if draw else 0.0
        tight = (draw_p >= 0.28) or (best_p < 0.50)
        if best_p >= 0.50 and not tight:
            candidates.append((best, "WINNER"))
        else:
            candidates.append((best, "SIDE"))
            lines = totals.get(mid, [])
            if lines:
                top = max(lines, key=lambda o: o.selection.model_probability)
                candidates.append((top, "TOTALS"))
        if tight and draw is not None:
            p_d = draw.selection.model_probability
            p_dc = min((p_home if p_home >= p_away else p_away) + p_d, 0.96)
            dc_sel = "1X" if p_home >= p_away else "X2"
            if p_dc >= 0.60:
                taken = round(1.0 / p_dc * DC_HAIRCUT, 2)
                if taken >= 1.05:
                    sel = Selection(match_id=mid,
                                    match_label=home.selection.match_label,
                                    league=league, market="DC",
                                    selection=dc_sel, model_probability=p_dc)
                    candidates.append((comparator.evaluate(sel, [OddsQuote(
                        book="derived", decimal_odds=taken)]), "DC"))

    seen: set = set()
    fresh: list[tuple[BetOpportunity, str]] = []
    for o, lane in candidates:
        k = (o.selection.match_id, o.selection.market, o.selection.selection)
        if k in pend_keys or o.selection.match_id in pend_matches or k in seen:
            continue
        seen.add(k)
        fresh.append((o, lane))

    fresh.sort(key=lambda t: t[0].selection.model_probability, reverse=True)
    picks = fresh[:remaining]

    print(f"\n  session {session.name}: {already}/{session.target} logged | "
          f"{len(fresh)} fresh | adding {len(picks)}\n")

    if not picks:
        msg = (f"{session.emoji} {session.name}: no NEW qualifying games this "
               f"cycle ({already}/{session.target} covered) - rescans continue.")
        print(msg)
        if tg.is_configured:
            tg.send(msg)
        return

    bankroll = Bankroll()
    count = 0
    for i, (opp, lane) in enumerate(picks, start=1):
        platform = PLATFORMS[count % len(PLATFORMS)]
        prob = opp.selection.model_probability
        label, mult = _tier(prob)
        eff = round(base_stake * mult, 2)
        slip = Slip(slip_type="SINGLE",
                    legs=[SlipLeg.from_opportunity(opp)], stake_units=eff)
        if not bankroll.can_place(eff):
            print(f"  !! exposure cap reached - placed {count}.")
            break
        bankroll.register_bet(eff)
        bet_id = logger.log_slip(slip, session=session.name)
        count += 1
        leg = slip.legs[0]
        floor = round(leg.decimal_odds * 0.97, 2)
        ko = _kickoff_dt(kmap.get(leg.match_id, ""))
        done = ((ko + timedelta(hours=sport_duration(opp.selection.league)))
                .astimezone(EAT).strftime("%H:%M EAT") if ko else "?")
        tag = " [MODEL]" if opp.selection.match_id in model_tagged else ""
        msg = (f"{lane}{tag} -> {platform.upper()} [{label}]\n"
               f"{leg.match_label.split(' · ')[0]}\n"
               f"KICKOFF {_kickoff_eat(kmap.get(leg.match_id, ''))} | "
               f"settles ~{done} (inside {session.name})\n"
               f"PICK: {_anchor(leg.market, leg.selection, leg.match_label)}\n"
               f"Win prob {prob:.0%} | take {leg.decimal_odds:.2f} | "
               f"place if app >= {floor}\n"
               f"Stake {eff:.2f}u | {bet_id}")
        print(msg + "\n")
        if tg.is_configured:
            tg.send(msg)

    total_now = _logged_this_session(logger, session.name, win_start)
    if tg.is_configured:
        tg.send(f"📊 {session.emoji} {session.name}: now {total_now}/"
                f"{session.target} covered. All settle before session end.")


if __name__ == "__main__":
    main()