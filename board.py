"""
board.py (FINAL) - the always-supplied scanner.

    * Odds band 1.50-2.75 (ledger-proven zone, slightly widened)
    * Exact-leg dedupe: a match with a pending SIDE bet can still yield
      a TOTALS/DC pick - flagged [PAIR] (correlated, second stake 0.5u)
    * Lanes per match: WINNER/SIDE + best O/U line + derived DC
    * FORM digest from data/history.csv (last 5 per team, zero credits)
    * S3 builds one 3-leg ACCA from the top singles (true combined
      probability printed - bigger payout, lower hit rate, honestly labeled)
    * Emits up to session.target picks; quiet-on-empty once per session
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
from odds.comparator import BetOpportunity, OddsComparator, Selection
from slips.generator import Slip, SlipLeg
from utils.bankroll import Bankroll
from utils.logger import BetLogger
from utils.session import EAT, current_or_next, clock_line

PLATFORMS: list[str] = ["BetPawa", "Betika", "LuckyPari", "WekaWin", "BetJam"]
BAND_MIN, BAND_MAX = 1.50, 2.75
CACHE = Path("data") / "feed_cache.json"
HISTORY = Path("data") / "history.csv"
STATE = Path("data") / "board_state.json"
DC_HAIRCUT = 0.95

DURATIONS = [
    ("mlb", 2.9), ("kbo", 2.9), ("npb", 2.9), ("baseball", 2.9),
    ("nfl", 3.2), ("ncaaf", 3.3), ("ncaab", 2.7), ("nba", 2.4),
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
    for k, h in DURATIONS:
        if k in s:
            return h
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


def _aliases() -> dict[str, str]:
    out: dict[str, str] = {}
    p = Path("data") / "team_aliases.csv"
    if p.exists():
        import csv
        with p.open("r", encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                a, b = (r.get("feed_name") or "").strip().lower(), \
                       (r.get("history_name") or "").strip().lower()
                if a and b:
                    out[a] = b
    return out


def _form_map() -> dict[str, list[tuple[str, int, int]]]:
    """team -> [(date, goals_for, goals_against)] newest last."""
    out: dict[str, list] = {}
    if not HISTORY.exists():
        return out
    import csv
    aliases = _aliases()

    def key(name: str) -> str:
        n = name.strip().lower()
        return aliases.get(n, n)

    with HISTORY.open("r", encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                hg, ag = int(r["home_goals"]), int(r["away_goals"])
            except (KeyError, TypeError, ValueError):
                continue
            d = (r.get("date") or "")
            h, a = key(r.get("home_team") or ""), key(r.get("away_team") or "")
            out.setdefault(h, []).append((d, hg, ag))
            out.setdefault(a, []).append((d, ag, hg))
    for team in out:
        out[team].sort(key=lambda x: x[0])
    return out


def _digest(form_map: dict, home: str, away: str) -> str:
    def five(team: str):
        games = form_map.get(team.strip().lower()) or []
        last = games[-5:]
        wdl = "".join("W" if gf > ga else ("D" if gf == ga else "L")
                      for _, gf, ga in last)
        gf = sum(g for _, g, _ in last)
        ga = sum(g for _, _, g in last)
        return f"{wdl or '?????'} {gf}:{ga}"

    return f"HOME FORM {five(home)} | AWAY FORM {five(away)}"


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
    if prob >= 0.75:
        return "HOT", 1.00
    if prob >= 0.65:
        return "WARM", 0.60
    if prob >= 0.55:
        return "STEADY", 0.40
    return "SPICY", 0.20


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
    print(f"  BOARD | {session.emoji} {session.name} | target {session.target} "
          f"| window ends {win_end.astimezone(EAT).strftime('%H:%M EAT')}")
    print(f"  {clock_line()}")
    print("=" * 70)

    cands = OddsApiFeed(min_hours_ahead=0.0, max_hours_ahead=hours,
                        markets="h2h,totals").collect()
    if not cands:
        msg = "[FAIL] feed returned nothing - run: python doctor.py"
        print(msg)
        if tg.is_configured:
            tg.send(msg)
        return

    comparator = OddsComparator(min_edge=0.0, min_ev_per_unit=0.0)
    pend_keys, pend_matches = _pending(logger)
    kmap = _kickoff_map()
    form_map = _form_map()

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
            continue
        m = opp.selection.market.upper()
        if m in ("1X2", "ML", "MONEYLINE"):
            h2h[opp.selection.match_id][opp.selection.selection] = opp
        elif m.startswith("O/U"):
            totals[opp.selection.match_id].append(opp)

    candidates: list[tuple[BetOpportunity, str, bool]] = []
    model_tagged: set[str] = set()

    for mid, sides in h2h.items():
        home, away, draw = (sides.get("Home"), sides.get("Away"),
                            sides.get("Draw"))
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

        # SIDE lane - in band only
        if BAND_MIN <= best.decimal_odds <= BAND_MAX:
            k_side = (mid, best.selection.market, best.selection.selection)
            if k_side not in pend_keys:
                candidates.append((best, "WINNER" if best_p >= 0.50 else "SIDE",
                                   best_p >= 0.50))

        # TOTALS lane - allowed even when side is pending ([PAIR])
        lines = totals.get(mid, [])
        if lines:
            top = max(lines, key=lambda o: o.selection.model_probability)
            if BAND_MIN <= top.decimal_odds <= BAND_MAX:
                k_ou = (mid, top.selection.market, top.selection.selection)
                if k_ou not in pend_keys:
                    candidates.append((top, "TOTALS", mid in pend_matches))

        # DC lane on tight matches (derived)
        if draw is not None:
            p_d = draw.selection.model_probability
            p_dc = min((p_home if p_home >= p_away else p_away) + p_d, 0.96)
            dc_sel = "1X" if p_home >= p_away else "X2"
            if p_dc >= 0.65:
                taken = round(1.0 / p_dc * DC_HAIRCUT, 2)
                if BAND_MIN <= taken <= BAND_MAX:
                    sel = Selection(match_id=mid,
                                    match_label=home.selection.match_label,
                                    league=league, market="DC",
                                    selection=dc_sel, model_probability=p_dc)
                    opp_dc = comparator.evaluate(sel, [Selection.__mro__ and __import__(
                        "odds.comparator", fromlist=["OddsQuote"]).OddsQuote(
                        book="derived", decimal_odds=taken)])
                    k_dc = (mid, "DC", dc_sel)
                    if k_dc not in pend_keys:
                        candidates.append((opp_dc, "DC", mid in pend_matches))

    seen: set = set()
    fresh: list[tuple[BetOpportunity, str, bool]] = []
    for o, lane, is_pair in candidates:
        k = (o.selection.match_id, o.selection.market, o.selection.selection)
        if k in seen:
            continue
        seen.add(k)
        fresh.append((o, lane, is_pair))

    fresh.sort(key=lambda t: t[0].selection.model_probability, reverse=True)
    picks = fresh[:session.target]

    print(f"\n  {len(fresh)} candidates in-window -> emitting {len(picks)}\n")

    if not picks:
        state = {}
        try:
            state = json.loads(STATE.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            pass
        today = datetime.now(timezone.utc).date().isoformat()
        if not (state.get("date") == today
                and state.get("quiet") == session.name):
            if tg.is_configured:
                tg.send(f"{session.emoji} {session.name}: no in-band games "
                        f"settle in this window - next cycle watches. "
                        f"credits ~{get_last_credits() or '?'}")
            state.update({"date": today, "quiet": session.name})
            STATE.write_text(json.dumps(state), encoding="utf-8")
        print("no in-band candidates this cycle")
        return

    bankroll = Bankroll()
    singles: list[tuple[str, BetOpportunity, str, str, float]] = []
    for i, (opp, lane, is_pair) in enumerate(picks, start=1):
        platform = PLATFORMS[(i - 1) % len(PLATFORMS)]
        prob = opp.selection.model_probability
        label, mult = _tier(prob)
        stake = base_stake * mult if not is_pair else round(base_stake * mult * 0.5, 2)
        slip = Slip(slip_type="SINGLE",
                    legs=[SlipLeg.from_opportunity(opp)], stake_units=stake)
        if not bankroll.can_place(stake):
            print(f"  !! exposure cap reached at pick {i}.")
            break
        bankroll.register_bet(stake)
        bet_id = logger.log_slip(slip, session=session.name)
        singles.append((platform, opp, bet_id, label, stake))
        leg = slip.legs[0]
        floor = round(leg.decimal_odds * 0.97, 2)
        ko = _kickoff_dt(kmap.get(leg.match_id, ""))
        done = ((ko + timedelta(hours=sport_duration(opp.selection.league)))
                .astimezone(EAT).strftime("%H:%M EAT") if ko else "?")
        hn, an = _teams(leg.match_label)
        form = _digest(form_map, hn, an)
        pair = " [PAIR: you hold the side bet on this match]" if is_pair else ""
        tag = " [MODEL]" if opp.selection.match_id in model_tagged else ""
        msg = (f"{lane}{tag} {i}/{len(picks)} -> {platform.upper()} [{label}]\n"
               f"{leg.match_label.split(' · ')[0]}\n"
               f"KICKOFF {_kickoff_eat(kmap.get(leg.match_id, ''))} | "
               f"settles ~{done} (inside {session.name})\n"
               f"PICK: {_anchor(leg.market, leg.selection, leg.match_label)}\n"
               f"Win prob {prob:.0%} | take {leg.decimal_odds:.2f} | "
               f"place if app >= {floor}\n"
               f"FORM: {form}{pair}\n"
               f"Stake {stake:.2f}u | credits ~{get_last_credits() or '?'} | {bet_id}")
        print(msg + "\n")
        if tg.is_configured:
            tg.send(msg)

    # ---- FAT-SESSION ACCA: one 3-leg slip from the top singles ----
    if session.name == "S3" and len(singles) >= 3:
        top3 = [s for _, o, _, _, _ in singles[:3]]
        odds_prod = 1.0
        prob_prod = 1.0
        for o in top3:
            odds_prod *= o.decimal_odds
            prob_prod *= o.selection.model_probability
        if prob_prod >= 0.08 and odds_prod <= 20.0:
            acca = Slip(slip_type="ACCA",
                        legs=[SlipLeg.from_opportunity(o) for o in top3],
                        stake_units=0.5)
            if bankroll.can_place(0.5):
                bankroll.register_bet(0.5)
                bet_id = logger.log_slip(acca, session=session.name)
                msg = (f"ACCA (optional - bigger payout, lower hit rate)\n"
                       + "\n".join(
                           f"  {l.match_label.split(' · ')[0]} -> "
                           f"{_anchor(l.market, l.selection, l.match_label)} "
                           f"@ {l.decimal_odds:.2f}"
                           for l in acca.legs)
                       + f"\nCombined odds {acca.combined_odds:.2f} | "
                         f"TRUE win prob {acca.combined_prob:.0%} "
                         f"(~{acca.combined_prob * 10:.0f} of 10)\n"
                         f"Stake 0.50u | {bet_id}")
                print(msg + "\n")
                if tg.is_configured:
                    tg.send(msg)

    if tg.is_configured:
        tot = sum(s for _, _, _, _, s in singles)
        tg.send(f"📦 {session.name}: {len(singles)} singles"
                f"{' + 1 ACCA' if session.name == 'S3' and len(singles) >= 3 else ''}"
                f" | stake {tot + (0.5 if session.name == 'S3' and len(singles) >= 3 else 0):.2f}u "
                f"| credits ~{get_last_credits() or '?'}")


if __name__ == "__main__":
    main()