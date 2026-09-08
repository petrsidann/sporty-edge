"""smart_picks.py (OWNER-SPEC FINAL — Concentrated Value pivot)
Per-session sport lists + per-session targets (5/5/8/5). All picks settle
before the session window ends. ODDS-BAND GATE: only picks priced 1.55-2.60
(the ledger's only profitable band, +9.8% ROI) are accepted; the sub-1.60
HIT lane is removed from selection (it bled -44.7%). Lanes: WINNER / SIDE /
TOTALS pivot on tight matches / DC derived (with the on-app confirmation
note). STAKES: flat 1.0u on every pick — tiers removed so the CLV
measurement stays clean. Dedupe: one bet per match. Hard cap = session
target minus already-logged-this-session. Team-strength + log5 blending
tagged [MODEL]. Telegram every pick; every pick message ends with
"credits ~N" (Phase 2b).
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

from feeds.oddsapi import OddsApiFeed, get_last_credits
from notify.telegram import TelegramNotifier
from config.settings import (
    CALIBRATION_SETTINGS,
    DC_DERIVED_MARGIN,
    HIT_RATE_SETTINGS,
    MAX_ODDS_TAKEN,
    MIN_ODDS_TAKEN,
    MIN_PLATFORM_SAMPLE,
    MODEL_BLEND_MODEL_WEIGHT,
    N_PICKS,
)
from odds.comparator import BetOpportunity, OddsComparator, OddsQuote, Selection
from slips.generator import Slip, SlipLeg
from utils.bankroll import Bankroll
from utils.logger import BetLogger
from utils.session import EAT, current_or_next, clock_line

PLATFORMS: list[str] = ["BetPawa", "Betika", "LuckyPari", "WekaWin", "BetJam"]
CACHE = Path("data") / "feed_cache.json"
DC_HAIRCUT = 0.95
DISCOVER_TS = Path("data") / "sport_discovery.json"

# Per-session sport keys (invalid keys 422-skip free of charge)
# Per-session sport selection: keyword-matched against DISCOVERED keys
# (data/valid_sports.json) - never guesses, never 404s.
SESSION_KEYWORDS: dict[str, tuple[str, ...]] = {
    # S1/S4 widened (Phase 1b): their native sports (MLB/NBA/NHL/NFL/A-League/
    # J-League/K-League/South America) currently have 0 events in feed_cache.
    # The cache only carries events for European soccer, tennis, euroleague,
    # handball. To keep the sessions live, S1/S4 fall back to those keys until
    # the next live refresh populates their native sports. Documented in
    # UPGRADE_NOTES.md.
    "S1": ("mlb", "nba", "nhl", "australia", "j_league", "japan",
           "kleague", "korea",
           "soccer_", "tennis_", "euroleague", "handball_"),
    "S2": ("russia", "poland", "czech", "hungary", "greece", "turkey",
           "tennis_", "serbia", "croatia", "romania",
           "soccer_", "euroleague", "handball_"),
    "S3": ("epl", "la_liga", "serie_a", "champs_league", "euroleague",
           "efl_champ", "bundesliga", "ligue_one", "eredivisie",
           "primeira_liga", "europa_league", "conference_league",
           "switzerland", "scotland_prem", "denmark", "sweden_allsvenskan",
           "norway_eliteserien", "belgium_first_div"),
    "S4": ("brazil", "argentina", "mexico", "mls", "nfl", "nba", "ncaaf",
           "mlb", "nhl",
           "soccer_", "tennis_", "euroleague", "handball_"),
}


def _session_sports(session_name: str) -> tuple[str, ...]:
    """Match discovered keys (key + title) against session keywords."""
    try:
        sports = json.loads(
            Path("data/valid_sports.json").read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return ()
    kws = SESSION_KEYWORDS.get(session_name, ())
    out = []
    for s in sports:
        key = (s.get("key") or "")
        title = (s.get("title") or "").lower()
        for kw in kws:
            if kw in key.lower() or kw in title:
                out.append(key)
                break
    return tuple(out)

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
    """Phase 1c: FLAT stakes — every pick stakes the same 1.0u.

    Tiers are removed: with fewer, sharper picks, uniform stakes keep the
    CLV and per-band ROI measurement unambiguous.  The label is descriptive
    only; the multiplier is always 1.00.
    """
    del prob  # argument kept for call-site compatibility; stakes never vary
    return "FLAT", 1.00


def _in_odds_band(odds: float) -> bool:
    """Phase 1a: the odds-band gate.

    A candidate is selectable only when its taken price lies inside
    [MIN_ODDS_TAKEN, MAX_ODDS_TAKEN] (1e-9 tolerance for float wobble).
    This is the concentrated-value pivot: 1.60-2.50 was the only
    profitable band in the ledger (+9.8% ROI), 1.20-1.60 the worst.
    """
    return (MIN_ODDS_TAKEN - 1e-9) <= odds <= (MAX_ODDS_TAKEN + 1e-9)


CREDITS_LAST = Path("data") / "credits_last.json"


def _save_credits_last() -> None:
    """Persist the last-seen API credits (Phase 2b).

    session_cycle.py runs smart_picks.py as a subprocess, so its own
    get_last_credits() is None; it reads this file instead.
    """
    c = get_last_credits()
    if c is None:
        return
    try:
        CREDITS_LAST.parent.mkdir(parents=True, exist_ok=True)
        CREDITS_LAST.write_text(
            json.dumps({"credits": int(c),
                        "ts": datetime.now(timezone.utc).isoformat()},
                       indent=2),
            encoding="utf-8")
    except OSError as exc:
        print(f"  !! could not write {CREDITS_LAST}: {exc!r}")


def _credits_line() -> str:
    """'credits ~N' tail for every Telegram pick message (Phase 2b)."""
    c = get_last_credits()
    return f"credits ~{c if c is not None else '?'}"


def build_pick_message(*, lane: str, model_tag: bool, platform: str,
                       label: str, match_label: str, kickoff_eat: str,
                       settle_eat: str, session_name: str, pick: str,
                       win_prob: float, take_odds: float, floor_odds: float,
                       stake: float, bet_id: str,
                       derived_dc: bool = False) -> str:
    """The Telegram pick message — the owner's placement contract.

    Every pick message MUST show: match, kickoff EAT, settle-by EAT, the
    exact pick, the price floor, the TRUE win probability, the stake, the
    bet id, and credits remaining (Phase 2b).  DC picks add the on-app
    confirmation note (the ledger price is the derived reference).
    """
    dc_note = ("\nDC derived from the 1X2 consensus (reference price) - "
               "confirm the real DC price on the app") if derived_dc else ""
    tag = " [MODEL]" if model_tag else ""
    return (f"{lane}{tag} -> {platform.upper()} [{label}]\n"
            f"{match_label.split(' · ')[0]}\n"
            f"KICKOFF {kickoff_eat} | settles ~{settle_eat} "
            f"(inside {session_name})\n"
            f"PICK: {pick}\n"
            f"Win prob {win_prob:.0%} | take {take_odds:.2f} | "
            f"place if app >= {floor_odds}\n"
            f"Stake {stake:.2f}u | {bet_id}"
            f"{dc_note}\n{_credits_line()}")


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


def _shrink_prob(prob: float) -> float:
    """Apply the calibration shrinkage factor (1.0 = no-op, default)."""
    factor = CALIBRATION_SETTINGS.shrinkage_factor
    if factor >= 1.0:
        return prob
    p = 0.5 + (prob - 0.5) * factor
    return min(max(p, 0.01), 0.99)


def _with_prob(opp: BetOpportunity, prob: float) -> BetOpportunity:
    """Copy of ``opp`` with a new model probability (dataclasses are frozen)."""
    return dc_replace(opp, selection=dc_replace(opp.selection, model_probability=prob))


def _blend_with_model(
    engine, opp: BetOpportunity
) -> tuple[BetOpportunity, bool]:
    """Blend consensus with the strength-engine model when BOTH teams are
    known: p = 0.4 * model + 0.6 * consensus (MODEL_BLEND_MODEL_WEIGHT).

    Returns (opportunity, blended_flag).  Unknown engine, unknown teams,
    unmodelled markets and any failure all fall back to pure consensus --
    blending is an upgrade, never a dependency.
    """
    if engine is None:
        return opp, False
    market = opp.selection.market.upper()
    pick = opp.selection.selection
    try:
        if market not in ("1X2", "ML") and not market.startswith(("O/U", "DC")):
            return opp, False
        home, away = _teams(opp.selection.match_label)
        if not engine.has(home, away):
            return opp, False
        xg = engine.expected_goals(home, away, opp.selection.league)
        model = PoissonMatchModel(xg)
        model_prob: float | None = None
        if market in ("1X2", "ML"):
            model_prob = model.one_x_two().get(pick)
        elif market.startswith("O/U"):
            line = float(market.split()[-1])
            model_prob = model.over_under(line).get(pick.lower())
        elif market.startswith("DC"):
            model_prob = model.double_chance().get(pick)
        if model_prob is None or not 0.0 < model_prob < 1.0:
            return opp, False
        blended = (MODEL_BLEND_MODEL_WEIGHT * model_prob
                   + (1.0 - MODEL_BLEND_MODEL_WEIGHT)
                   * opp.selection.model_probability)
        if not 0.01 <= blended <= 0.99:
            return opp, False
        return _with_prob(opp, blended), True
    except Exception:
        # The model must never cost a session: consensus stands.
        return opp, False


def _dc_candidates(h2h: dict) -> list[tuple[BetOpportunity, str]]:
    """Double-chance candidates DERIVED from the 1X2 consensus.

    The feed carries no DC quotes, so the reference price is derived from
    the devigged consensus: ref = 1/p * (1 - DC_DERIVED_MARGIN), then the
    usual 3% price floor applies on top.  This is disclosed on the pick
    message so the owner verifies the real DC price on the app.
    """
    out: list[tuple[BetOpportunity, str]] = []
    comparator = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0)
    for sides in h2h.values():
        h, d, a = (sides.get("Home"), sides.get("Draw"), sides.get("Away"))
        if not (h and d and a):
            continue  # 2-way sport: no double chance exists
        for pick, opps in (("1X", (h, d)), ("X2", (d, a)), ("12", (h, a))):
            p = sum(o.selection.model_probability for o in opps)
            if not (HIT_RATE_SETTINGS.min_prob - 1e-9 <= p <= 0.995):
                continue
            derived = (1.0 / p) * (1.0 - DC_DERIVED_MARGIN)
            if derived > HIT_RATE_SETTINGS.max_odds + 1e-9:
                continue
            sel = dc_replace(opps[0].selection, market="DC", selection=pick,
                             model_probability=p)
            out.append((comparator.evaluate(
                sel, [OddsQuote(book="derived-1X2", decimal_odds=round(derived, 3))]
            ), "HIT"))
    return out


def _hit_candidates(h2h: dict, totals: dict) -> list[tuple[BetOpportunity, str]]:
    """All [HIT]-eligible candidates: probability >= min_prob, odds <= cap.

    Sources: moneylines with a strong consensus, the low/high totals lines
    (Over 0.5 / Over 1.5 / Under 4.5 / Under 5.5 by default), and derived
    double chance.  Ranking and per-match dedupe happen in main().
    Thresholds tolerate a 1e-9 float wobble (0.70 + 0.10 < 0.80 in binary).
    """
    out: list[tuple[BetOpportunity, str]] = []
    p_min = HIT_RATE_SETTINGS.min_prob - 1e-9
    o_max = HIT_RATE_SETTINGS.max_odds + 1e-9

    for sides in h2h.values():
        for opp in sides.values():
            if opp.selection.model_probability >= p_min \
                    and opp.decimal_odds <= o_max:
                out.append((opp, "HIT"))

    hit_lines = set(HIT_RATE_SETTINGS.lines)
    for lines in totals.values():
        for opp in lines:
            market = opp.selection.market.upper()
            try:
                line = float(market.split()[-1])
            except (ValueError, IndexError):
                continue
            if line in hit_lines and opp.selection.model_probability >= p_min \
                    and opp.decimal_odds <= o_max:
                out.append((opp, "HIT"))

    out.extend(_dc_candidates(h2h))
    return out


def platform_slots(
    performance: dict[str, dict],
    platforms: list[str],
    n_slots: int = N_PICKS,
    min_sample: int = MIN_PLATFORM_SAMPLE,
) -> list[str]:
    """The ``n_slots`` platform assignments for one session.

    ``performance`` maps platform -> {"n", "wins", "staked", "profit"} from
    the ledger.  Platforms with >= min_sample settled bets are ranked by
    win rate (then ROI -- the owner's goal is the hit rate, profit breaks
    ties); the top 3 get two picks, the rest get one.  Platforms without
    enough history keep the default order.  Pure function: testable
    without a feed or a ledger.
    """
    targets = list(platforms)
    if not targets:
        return []
    measured = []
    for name in targets:
        stats = performance.get(name) or {}
        settled = int(stats.get("n") or 0)
        if settled >= min_sample and stats.get("staked"):
            wins = int(stats.get("wins") or 0)
            roi = float(stats.get("profit") or 0.0) / float(stats["staked"])
            measured.append((name, wins / settled, roi))
    ranked = [name for name, _wr, _roi in
              sorted(measured, key=lambda t: (t[1], t[2]), reverse=True)]
    ordered = ranked + [p for p in targets if p not in ranked]

    slots: list[str] = []
    doubles = 3 if len(ordered) >= 5 else len(ordered)
    while len(slots) < n_slots:
        for i, name in enumerate(ordered):
            if len(slots) >= n_slots:
                break
            slots.append(name)
            if i >= doubles:
                continue
            if len(slots) < n_slots:
                slots.append(name)
    return slots


def _platform_performance(logger: BetLogger) -> dict[str, dict]:
    """Per-platform settled record from the ledger (platform recorded at
    log time by smart_picks, or via attach_code)."""
    perf: dict[str, dict] = {}
    for rec in logger._read_all():
        plat = rec.get("platform")
        status = rec.get("status")
        if not plat or status not in ("WIN", "LOSS"):
            continue
        d = perf.setdefault(str(plat), {"n": 0, "wins": 0,
                                        "staked": 0.0, "profit": 0.0})
        d["n"] += 1
        d["staked"] += float(rec.get("stake_units") or 0.0)
        d["profit"] += float(rec.get("profit_units") or 0.0)
        if status == "WIN":
            d["wins"] += 1
    return perf


def main() -> None:
    # Phase 1c: flat 1.0u per pick (--stake overrides for experiments only).
    base_stake = 1.0
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

    sports = list(_session_sports(session.name))
    if session.name in ("S2", "S3"):
        sports.extend(_discover_tennis())

    cands = OddsApiFeed(min_hours_ahead=0.0, max_hours_ahead=hours,
                        sports=tuple(sports),
                        markets="h2h,totals").collect()
    _save_credits_last()  # Phase 2b: credits known after any feed/cache run
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
    gated = 0
    for o, lane in candidates:
        k = (o.selection.match_id, o.selection.market, o.selection.selection)
        if k in pend_keys or o.selection.match_id in pend_matches or k in seen:
            continue
        seen.add(k)
        if not _in_odds_band(o.decimal_odds):
            gated += 1  # concentrated-value pivot: outside 1.55-2.60
            continue
        fresh.append((o, lane))

    fresh.sort(key=lambda t: t[0].selection.model_probability, reverse=True)
    picks = fresh[:remaining]

    print(f"\n  session {session.name}: {already}/{session.target} logged | "
          f"{len(fresh)} fresh | adding {len(picks)}")
    if gated:
        print(f"  odds-band gate [{MIN_ODDS_TAKEN}-{MAX_ODDS_TAKEN}]: "
              f"{gated} candidate(s) outside the profitable band rejected "
              f"(flat 1.0u stakes on survivors)")

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
        msg = build_pick_message(
            lane=lane, model_tag=opp.selection.match_id in model_tagged,
            platform=platform, label=label,
            match_label=leg.match_label,
            kickoff_eat=_kickoff_eat(kmap.get(leg.match_id, "")),
            settle_eat=done, session_name=session.name,
            pick=_anchor(leg.market, leg.selection, leg.match_label),
            win_prob=prob, take_odds=leg.decimal_odds, floor_odds=floor,
            stake=eff, bet_id=bet_id,
            derived_dc=(leg.market.upper() == "DC"))
        print(msg + "\n")
        if tg.is_configured:
            tg.send(msg)

    total_now = _logged_this_session(logger, session.name, win_start)
    if tg.is_configured:
        tg.send(f"📊 {session.emoji} {session.name}: now {total_now}/"
                f"{session.target} covered. All settle before session end. "
                f"{_credits_line()}")


if __name__ == "__main__":
    main()